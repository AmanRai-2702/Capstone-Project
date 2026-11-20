from pydantic import BaseModel, Field


class ImpactHop(BaseModel):
    """One step of the chain: `dependent` depends on `dependency` through `relation`."""
    dependent: str
    dependency: str
    relation: str          # raw relationship type that created the dependency (CALLS, HANDLES...)
    kind: str              # detected | inferred | heuristic
    confidence: float
    evidence: str = ""     # code snippet, if the analyzer recorded one
    location: str | None = None


class ImpactedComponent(BaseModel):
    entity_id: str
    name: str
    type: str
    language: str
    location: str | None = None
    distance: int                       # 1 = direct dependent, 2+ = transitive
    # direct         - depends on the changed component itself
    # execution_flow - reached only through execution edges (CALLS/REQUESTS/HANDLES/TRIGGERS)
    # indirect       - reached through structural edges too (IMPORTS/REFERENCES/STYLES/INHERITS/USES)
    category: str
    uncertain: bool                     # some hop is inferred/heuristic, or confidence < 0.8
    confidence: float                   # product of hop confidences along the chain
    has_inferred_hop: bool
    chain: list[ImpactHop]              # ordered from this component down to the changed one
    explanation: str


class ImpactReport(BaseModel):
    target: str                         # what the user asked about (or a label for a diff)
    changed: list[str]                  # entity ids treated as changed (target + contained entities)
    direct: list[ImpactedComponent] = Field(default_factory=list)
    transitive: list[ImpactedComponent] = Field(default_factory=list)
    blast_radius: int = 0               # number of impacted components
    by_language: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    affected_entry_points: list[str] = Field(default_factory=list)  # UI elements / endpoints hit
    suggested_tests: list[str] = Field(default_factory=list)        # test files that touch impacted code
    risk: str = "low"                   # low | medium | high  (heuristic, see ImpactService)
    risk_reason: str = ""

    @property
    def impacted(self) -> list[ImpactedComponent]:
        return self.direct + self.transitive
