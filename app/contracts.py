from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.discovery import propose_creator_queries


class TaskCard(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=200)
    sample_count: int = Field(default=5, ge=1, le=50)
    min_avg_likes: int = Field(default=0, ge=0, le=1_000_000_000_000)
    min_followers: int = Field(default=0, ge=0, le=1_000_000_000_000)
    max_followers: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    min_max_likes: int = Field(default=0, ge=0, le=1_000_000_000_000)
    active_days: int | None = Field(default=None, ge=1, le=3650)
    content_preferences: list[str] = Field(default_factory=list, max_length=20)
    exclusions: list[str] = Field(default_factory=list, max_length=20)
    # Legacy modes remain readable; new executions use Xingtu in the API.
    discovery_mode: Literal['xingtu', 'user', 'hybrid', 'video'] = 'xingtu'
    # Broad food terms get role-based recall queries before confirmation.
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    # Initial engineering policy, configurable per task; not an industry norm.
    topic_min_samples: int = Field(default=5, ge=1, le=10)
    topic_min_ratio: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def follower_range(self):
        if self.max_followers is not None and self.max_followers < self.min_followers:
            raise ValueError('粉丝上限不能小于下限')
        queries = [str(item).strip() for item in self.search_queries if str(item).strip()]
        if any(len(item) > 200 for item in queries):
            raise ValueError('每个查询词须为 1–200 字')
        if len(self.search_queries) > 3:
            raise ValueError('查询词最多 3 个')
        if self.search_queries and not queries:
            raise ValueError('查询词不能为空')
        self.search_queries = propose_creator_queries(self.keyword, queries, self.content_preferences)
        if any(len(item) > 200 for item in self.search_queries):
            raise ValueError('每个查询词须为 1–200 字')
        return self


class ChatOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    intent: Literal['chat', 'clarify', 'search']
    reply: str = Field(min_length=1, max_length=8000)
    task_card: TaskCard | None = None

    @model_validator(mode='after')
    def check_executable_card(self):
        if self.intent == 'search' and self.task_card is None:
            raise ValueError('search requires a task card')
        if self.intent != 'search':
            self.task_card = None
        return self


class NumericAssessment(BaseModel):
    """Shared three-state numeric gate used by crawler, workflow and report."""

    model_config = ConfigDict(extra='forbid')

    status: Literal['passed', 'failed', 'unknown']
    failed_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class TopicReviewDecision(BaseModel):
    """One model decision; server-side code recomputes every count."""

    model_config = ConfigDict(extra='forbid')

    aweme_id: str = Field(min_length=1, max_length=80)
    decision: Literal['yes', 'no', 'unknown']
    evidence_id: str = Field(default='', max_length=80)
    reason: str = Field(default='', max_length=300)


class RequirementDecision(BaseModel):
    """Whether one explicit condition is satisfied, including exclusions."""

    model_config = ConfigDict(extra='forbid')

    requirement_id: str = Field(min_length=1, max_length=80)
    decision: Literal['matched', 'not_matched', 'unknown']
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default='', max_length=300)


class AccountIdentity(BaseModel):
    """Account role is separate from whether its posts mention the target topic."""

    model_config = ConfigDict(extra='forbid')

    account_type: Literal['independent_creator', 'creator_team', 'brand_store', 'institution', 'unknown'] = 'unknown'
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(default='', max_length=500)


class TopicReviewOutput(BaseModel):
    """Strict output protocol returned by the qualification prompt."""

    model_config = ConfigDict(extra='forbid')

    posts: list[TopicReviewDecision] = Field(default_factory=list, max_length=20)
    condition_decisions: list[RequirementDecision] = Field(default_factory=list, max_length=40)
    creator_pattern: Literal['consistent_creator', 'occasional_poster', 'uncertain'] = 'uncertain'
    account: AccountIdentity = Field(default_factory=AccountIdentity)
    reason: str = Field(default='', max_length=1000)


class TopicAssessment(BaseModel):
    """Auditable text review; it never claims to have watched a video."""

    model_config = ConfigDict(extra='forbid')

    topic_status: Literal['matched', 'not_matched', 'unknown']
    method: Literal['text_llm'] = 'text_llm'
    posts: list[TopicReviewDecision] = Field(default_factory=list)
    condition_decisions: list[RequirementDecision] = Field(default_factory=list)
    required_condition_ids: list[str] = Field(default_factory=list)
    creator_pattern: Literal['consistent_creator', 'occasional_poster', 'uncertain'] = 'uncertain'
    account: AccountIdentity = Field(default_factory=AccountIdentity)
    sample_count: int = Field(default=0, ge=0, le=20)
    matched_count: int = Field(default=0, ge=0, le=20)
    unknown_count: int = Field(default=0, ge=0, le=20)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default='', max_length=1000)
    prompt_version: str = ''
    model: str = ''
    error_code: str | None = None
    truncated_ids: list[str] = Field(default_factory=list)
    ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    min_samples: int = Field(default=5, ge=1)
    min_ratio: float = Field(default=0.6, ge=0.0, le=1.0)
