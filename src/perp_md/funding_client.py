from __future__ import annotations

from collections.abc import Iterable, Mapping

from cdm import (
    DataPointDefinitionV1,
    DataPointKind,
    FundingIntervalKind,
    InstrumentDescriptorV1,
    InstrumentReferenceV1,
    NativeIdentityV1,
    TemporalMode,
)

from perp_md.adapters.base import FundingAdapter
from perp_md.adapters.ccxt_funding import CcxtFundingAdapter
from perp_md.adapters.funding import native_funding_adapters
from perp_md.capabilities import (
    AcquisitionPlan,
    CapabilityAssessment,
    CapabilityIssue,
    CapabilityStatus,
    assess_capability,
    planned_retrieval,
)
from perp_md.errors import AdapterUnavailable, CapabilityUnavailable
from perp_md.identity import ReferenceInstrument
from perp_md.models import (
    FundingCapabilities,
    FundingResult,
    HistoryRange,
    Instrument,
)
from perp_md.transport import HttpxTransport, JsonTransport


class FundingClient:
    """Typed funding acquisition client independent from open-interest state."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        request_concurrency: int = 16,
        per_host_concurrency: int = 4,
        transport: JsonTransport | None = None,
        adapters: Mapping[str, FundingAdapter] | None = None,
        enable_ccxt_fallback: bool = False,
        fallback: FundingAdapter | None = None,
    ) -> None:
        self._owns_transport = transport is None
        self._transport = transport or HttpxTransport(
            timeout_seconds,
            request_concurrency,
            per_host_concurrency,
        )
        self._adapters = {
            key.upper(): value
            for key, value in (
                adapters or native_funding_adapters(self._transport)
            ).items()
        }
        self._fallback = fallback or (
            CcxtFundingAdapter(timeout_seconds) if enable_ccxt_fallback else None
        )
        self._runtime_feature_cache: dict[tuple[int, str], frozenset[str]] = {}
        self._closed = False

    def capabilities(self, instrument: Instrument) -> FundingCapabilities:
        return self._select(instrument).capabilities(instrument)

    def assess(
        self,
        provider_id: str,
        descriptor: InstrumentDescriptorV1 | InstrumentReferenceV1 | Instrument,
        *,
        native_identities: Iterable[NativeIdentityV1] = (),
        datapoint: DataPointKind
        | DataPointDefinitionV1 = DataPointKind.FUNDING_INDICATIVE_RATE,
        temporal_mode: TemporalMode | None = None,
        market_observations: Iterable[str] | None = None,
        runtime_features: Iterable[str] = (),
    ) -> CapabilityAssessment:
        """Assess built-in behavior without requiring a complete instrument."""
        return assess_capability(
            provider_id,
            datapoint,
            descriptor,
            native_identities=native_identities,
            temporal_mode=temporal_mode,
            market_observations=market_observations,
            runtime_features=runtime_features,
        )

    async def runtime_features(
        self,
        provider_id: str,
        reference: InstrumentReferenceV1,
    ) -> frozenset[str]:
        """Return exact features proven by the configured adapter runtime."""

        if self._closed:
            raise RuntimeError("client is closed")
        instrument = ReferenceInstrument(_provider_id(provider_id), reference)
        adapter = self._select(instrument)
        cache_key = (id(adapter), instrument.venue)
        cached = self._runtime_feature_cache.get(cache_key)
        if cached is not None:
            return cached
        probe = getattr(adapter, "runtime_features", None)
        if probe is None:
            features = frozenset()
        else:
            features = frozenset(await probe(instrument))
        self._runtime_feature_cache[cache_key] = features
        return features

    async def assess_runtime(
        self,
        provider_id: str,
        reference: InstrumentReferenceV1,
        *,
        datapoint: DataPointKind
        | DataPointDefinitionV1 = DataPointKind.FUNDING_INDICATIVE_RATE,
        temporal_mode: TemporalMode | None = None,
        market_observations: Iterable[str] | None = None,
    ) -> CapabilityAssessment:
        """Assess the installed adapter without exposing its feature identifiers."""

        provider = _provider_id(provider_id)
        try:
            features = await self.runtime_features(provider, reference)
        except AdapterUnavailable:
            kind = (
                datapoint.kind
                if isinstance(datapoint, DataPointDefinitionV1)
                else datapoint
            )
            temporal = (
                datapoint.temporal_mode
                if isinstance(datapoint, DataPointDefinitionV1)
                else temporal_mode
            )
            return CapabilityAssessment(
                provider,
                kind,
                temporal,
                CapabilityStatus.ADAPTER_UNAVAILABLE,
                (
                    CapabilityIssue(
                        "adapter_unavailable",
                        "$.runtime.adapter",
                        "no configured adapter can serve this provider",
                    ),
                ),
            )
        return assess_capability(
            provider,
            datapoint,
            reference,
            temporal_mode=temporal_mode,
            market_observations=market_observations,
            runtime_features=features,
        )

    async def fetch_reference(
        self,
        provider_id: str,
        reference: InstrumentReferenceV1,
        history: HistoryRange | None = None,
        *,
        include_history: bool = True,
    ) -> FundingResult:
        """Fetch from an unchanged CDM reference after structured preflight."""

        if self._closed:
            raise RuntimeError("client is closed")
        assessments = [
            await self.assess_runtime(
                provider_id,
                reference,
                datapoint=kind,
                temporal_mode=temporal,
            )
            for kind, temporal in (
                (DataPointKind.FUNDING_INDICATIVE_RATE, TemporalMode.CURRENT),
                (DataPointKind.FUNDING_NEXT_RATE, TemporalMode.NEXT),
                (DataPointKind.FUNDING_SETTLED_RATE, TemporalMode.SETTLED),
            )
        ]
        if not any(item.status is CapabilityStatus.SUPPORTED for item in assessments):
            raise CapabilityUnavailable(_best_assessment(assessments))
        if include_history:
            historical = await self.assess_runtime(
                provider_id,
                reference,
                datapoint=DataPointKind.FUNDING_SETTLED_RATE,
                temporal_mode=TemporalMode.HISTORICAL,
            )
            if any(
                issue.code in {"missing_native_identity", "ambiguous_native_identity"}
                for issue in historical.issues
            ):
                raise CapabilityUnavailable(historical)
        instrument = ReferenceInstrument(_provider_id(provider_id), reference)
        return await self._select(instrument).fetch(
            instrument,
            history,
            include_history=include_history,
        )

    async def plan_reference(
        self,
        provider_id: str,
        reference: InstrumentReferenceV1,
        *,
        datapoint: DataPointKind
        | DataPointDefinitionV1 = DataPointKind.FUNDING_INDICATIVE_RATE,
        temporal_mode: TemporalMode = TemporalMode.CURRENT,
        market_observations: Iterable[str] | None = None,
    ) -> AcquisitionPlan:
        """Return runtime-assessed scheduler inputs without adapter internals."""

        assessment = await self.assess_runtime(
            provider_id,
            reference,
            datapoint=datapoint,
            temporal_mode=temporal_mode,
            market_observations=market_observations,
        )
        instrument = ReferenceInstrument(_provider_id(provider_id), reference)
        try:
            capabilities = self._select(instrument).capabilities(instrument)
        except AdapterUnavailable:
            return planned_retrieval(assessment)
        interval = capabilities.declared_interval
        fixed_interval_seconds = (
            interval.duration_seconds
            if interval is not None
            and interval.kind is FundingIntervalKind.EXPLICIT_DURATION
            else None
        )
        return planned_retrieval(
            assessment,
            fixed_interval_seconds=fixed_interval_seconds,
            requires_explicit_start=capabilities.history_requires_start,
        )

    async def fetch(
        self,
        instrument: Instrument,
        history: HistoryRange | None = None,
        *,
        include_history: bool = True,
    ) -> FundingResult:
        if self._closed:
            raise RuntimeError("client is closed")
        return await self._select(instrument).fetch(
            instrument,
            history,
            include_history=include_history,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runtime_feature_cache.clear()
        seen: set[int] = set()
        for adapter in [*self._adapters.values(), self._fallback]:
            if adapter is not None and id(adapter) not in seen:
                seen.add(id(adapter))
                await adapter.close()
        if self._owns_transport:
            await self._transport.close()

    async def __aenter__(self) -> "FundingClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _select(
        self,
        instrument: Instrument | ReferenceInstrument,
    ) -> FundingAdapter:
        adapter = self._adapters.get(instrument.venue)
        if adapter is not None and adapter.supports(instrument):
            return adapter
        if self._fallback is not None and self._fallback.supports(instrument):
            return self._fallback
        raise AdapterUnavailable("no funding adapter is configured for this provider")


def _best_assessment(
    assessments: list[CapabilityAssessment],
) -> CapabilityAssessment:
    rank = {
        CapabilityStatus.METADATA_INCOMPLETE: 0,
        CapabilityStatus.RUNTIME_UNAVAILABLE: 1,
        CapabilityStatus.ADAPTER_UNAVAILABLE: 2,
        CapabilityStatus.UNSUPPORTED: 3,
        CapabilityStatus.SUPPORTED: 4,
    }
    return min(assessments, key=lambda item: rank[item.status])


def _provider_id(value: str) -> str:
    provider = value.strip().upper()
    if not provider:
        raise ValueError("provider_id is required")
    return provider
