# ADR-0002: Plugin-Contributed System Prompt Extensions

**Status:** Proposed

**Date:** 2026-05-12

**Applies to:** deriva-mcp-core, deriva-mcp-ui

---

## Context

The deriva-mcp-ui chatbot constructs an LLM system prompt on the first turn of
each conversation. The prompt is currently assembled from four sources:

1. A base persona string (`chat.py:system_prompt`, ~500 chars).
2. Nine numbered mandatory rules inline in `chat.py:135-298` (~4-6k chars).
3. Four "guide prompts" fetched from the MCP server by hardcoded name
   (`query_guide`, `entity_guide`, `annotation_guide`, `catalog_guide`),
   concatenated into a `guide_context` block (~15-25k chars combined).
4. Optional schema priming (up to 20k chars) and ERMrest RAG context.

The four guide prompts are registered in `deriva-mcp-core` via
`ctx.prompt(name, ...)` in `tools/prompts.py`. The UI's `_GUIDE_PROMPT_NAMES`
list is hardcoded and unaware of any other prompts a plugin might register.

Two pressures are now pushing on this design:

**Plugins need to contribute system-prompt content.** A concrete case is
`deriva-ml-mcp`, a plugin that wraps DerivaML workflows and needs to teach the
LLM how to use its tools (ML training, prediction, dataset orchestration).
Today the only way to extend the system prompt is to edit `chat.py` directly,
which makes the UI repo a dumping ground for plugin-specific guidance and
violates plugin boundaries.

**The system prompt is already too large for the operating envelope.** The
chatbot runs against multiple model tiers via litellm: Anthropic Haiku
(production cost tier), Anthropic Opus/Sonnet (high-capability tier), and
self-hosted local models. Haiku and local models degrade noticeably as the
system prompt grows past ~10-15k tokens -- both in instruction-following
quality and in following multi-step directives like "call X before doing Y."
The current prompt is already pushing 30-50k tokens before any plugin
contribution. Adding more eager content per plugin is unsustainable.

The MCP protocol itself was designed for *lazy* prompt loading: prompts are
templates fetched on demand via `prompts/get`. The header comment in
`tools/prompts.py` reflects this intent ("the LLM loads the guidance once per
conversation rather than carrying it in every tool call"), but the UI ignored
that intent and eager-loads everything. We are paying for that drift now.

---

## Decision

Introduce a structured **system prompt extension** abstraction. Plugins
*declare* contributions with metadata. The UI carries a *strategy* layer that
decides what to inject based on the model tier, the connected catalog, and a
token budget. The two concerns are separated so policy can change without
plugin code changes.

### Core principles

1. **Mechanism is uniform, policy is swappable.** Plugins describe what they
   contribute and its properties. The UI decides how to use it.
2. **Conditional scoping is deterministic, not model-dependent.** A plugin
   that only applies to certain catalogs declares that in metadata; the UI
   filters before the LLM ever sees the prompt.
3. **Built-in guidance uses the same mechanism.** The four current guides and
   the inline mandatory rules in `chat.py` are migrated into the same
   extension API, so there is no "built-in vs plugin" asymmetry.
4. **MCP-native transport.** Extension metadata is exposed via a standard
   MCP resource (`deriva://prompt-extensions`); bodies are fetched via
   standard `prompts/get`. No new RPC surface.

---

## Detailed design

### 1. Plugin API

A single new method on `PluginContext`:

```python
ctx.system_prompt_extension(
    name: str,
    content: str,
    *,
    category: str = "tool_guide",
    priority: int = 50,
    applies_to: AppliesTo | None = None,
    estimated_tokens: int | None = None,
) -> None
```

**Field semantics:**

- `name` -- Unique identifier across all plugins and built-ins. Used as the
  MCP prompt name and as the manifest entry identifier. Convention:
  snake_case, prefixed by plugin namespace for non-built-in contributions
  (e.g. `ml_workflow`, `ml_training_guide`).
- `content` -- The text body. Plain ASCII per project convention.
- `category` -- One of `base_rules`, `tool_guide`, `domain_rules`,
  `display_rules`, `safety`. Used by strategies to make per-category
  decisions (e.g. always inject `safety`, defer `tool_guide`).
- `priority` -- 0-100. Higher wins when a budgeted strategy has to drop
  candidates. Built-in `base_rules` use 90+; plugin guides default to 50.
- `applies_to` -- Optional scope filter. Concrete types:
  - `AppliesTo.catalog(hostname=..., catalog_id=...)` -- match exact catalog
  - `AppliesTo.hostname_pattern("*.facebase.org")` -- glob match
  None means "always applies." Strategies filter the candidate set by
  `applies_to` before any other decision.
- `estimated_tokens` -- Plugin author's estimate. If absent, the loader
  computes `len(content) // 4` as a rough proxy. Used by budgeted
  strategies; not enforced.

### 2. Core changes

**Collection in PluginContext.** `system_prompt_extension` appends to
`self._extensions: list[ExtensionDeclaration]`. Each declaration also gets
auto-registered as a standard MCP prompt via `self._mcp.prompt(name)`, so
the existing `prompts/get` transport works unchanged for fetching bodies.

**Provenance tracking.** Each `ExtensionDeclaration` carries an internal
field `source: Literal["builtin", "plugin"]`, set by core at registration
time -- plugin authors cannot influence it. Extensions registered from
`tools/*.py` get `"builtin"`; extensions registered via entry-point
plugins (`plugin/loader.py`) get `"plugin"`. The field is not exposed in
the public API or the manifest resource; it is used internally by the
selection strategy and by startup-time validation.

**Startup validation.** Plugin registrations are validated when the
plugin loader runs. If a plugin registers an extension with
`priority >= 90`, the loader logs ERROR and skips the registration.
Priority >=90 is reserved for built-in `base_rules` and built-in
mandatory content; plugin extensions are capped below this floor by
construction. Fail loudly at startup rather than silently in production.

**Manifest resource.** Core registers an MCP resource at
`deriva://prompt-extensions` that returns the full manifest as structured
JSON:

```json
{
  "extensions": [
    {
      "name": "query_guide",
      "category": "tool_guide",
      "priority": 70,
      "applies_to": null,
      "estimated_tokens": 1800,
      "summary": "ERMrest query patterns, path syntax, pagination..."
    }
  ]
}
```

The UI fetches this resource once on first turn, then fetches bodies it
needs via standard `prompts/get`. Plugin authors call only
`ctx.system_prompt_extension(...)`; core handles both the resource entry
and the prompt registration behind the scenes. No string-sentinel
convention; metadata is structured and schema-checkable.

**Built-in migration.** `tools/prompts.py` is rewritten to use
`ctx.system_prompt_extension` for the four existing guides, each with
`category="tool_guide"`, `priority=70` (preserving current behavior under
the budgeted strategy). The nine mandatory rules currently in
`chat.py:135-298` are moved into core as a `base_rules` extension with
`category="base_rules"`, `priority=95`. This is the heart of the
migration -- it puts all guidance content under one mechanism.

**`get_guide` tool.** A new built-in MCP tool, registered in
`tools/prompts.py`:

```python
@ctx.tool(mutates=False)
async def get_guide(name: str) -> str:
    """Fetch the full text of a system prompt extension by name.

    Use this when you encounter an unfamiliar tool or workflow and the
    system prompt's manifest section indicates that a guide is available.
    Call this once per conversation per guide; the content does not change.
    """
```

This tool exists primarily for the manifest strategy. The budgeted
strategy will rarely see it called, but the tool docstrings of plugin
tools can reference it consistently regardless of which strategy is active.

### 3. UI changes

**Discovery.** On first turn the UI reads the `deriva://prompt-extensions`
resource and parses the JSON into a list of `ExtensionDescriptor` objects.
Bodies are fetched only for the subset the strategy decides to inject, via
standard `prompts/get`.

**Strategy interface.** A single small interface in
`deriva_mcp_ui/chat.py` (or a new `prompt_strategy.py`):

```python
class PromptInjectionStrategy(Protocol):
    async def select(
        self,
        extensions: list[ExtensionDescriptor],
        session: Session,
        settings: Settings,
        mcp_session: ClientSession,
    ) -> SelectedPrompt:
        ...

@dataclass
class ManifestEntry:
    name: str
    summary: str

@dataclass
class SelectedPrompt:
    injected_text: str                       # full text to splice into the system prompt
    manifest_entries: list[ManifestEntry]    # advertised in the prompt's manifest section
    on_demand_names: list[str]               # set of names valid for get_guide() lookup
```

**Mandatory core (shared).** Both strategies unconditionally inject
every extension where `category in {base_rules, safety, display_rules}`,
after filtering by `applies_to`. These categories carry the rules the
model cannot reliably operate without; they are not budget-bound under
either strategy.

**Built-in protection (strategy-specific).** Under `BudgetedStrategy`,
built-in `tool_guide` and `domain_rules` extensions (`source == "builtin"`)
also inject unconditionally and do not consume from the budget. Only
plugin-contributed optional content is budget-bound. This guarantees
plugins cannot displace built-in guides under budget pressure. Under
`ManifestStrategy`, this distinction does not apply -- nothing is budget
bound there, and built-in optional content is advertised in the manifest
alongside plugin content (see strategy description).

**Two initial strategies:**

`BudgetedStrategy(max_tokens=8000)`:
- Includes the mandatory core (not counted against `max_tokens`).
- From the *plugin-contributed* `tool_guide` and `domain_rules`
  extensions (after `applies_to` filtering), sorts by priority
  descending; fetches bodies in order and packs them into the system
  prompt until adding the next would exceed `max_tokens`.
- `max_tokens` caps only plugin-contributed optional content. Built-in
  guides and mandatory categories inject unconditionally and do not
  consume from this budget.
- Plugin extensions that don't fit are skipped (not partially included)
  and logged at WARNING with category, priority, and estimated cost.
- Built-in `tool_guide` / `domain_rules` are injected unconditionally
  before plugin packing begins (they are not budget-bound). If the
  built-in set alone is so large that the operator can no longer fit
  expected plugin content, that is a core/operator config problem and
  is surfaced via the standard drop-logging on plugin extensions.
- Default `8000` is sized to fit a handful of plugin-contributed
  optional guides on top of the built-in baseline. Operators tune it
  per deployment based on model context window and cost budget.
- Intended for Haiku and weaker local models, where multi-step
  "call X before doing Y" patterns are unreliable and content needs
  to be in front of the model up front.

`ManifestStrategy()`:
- Includes the mandatory core (`base_rules`, `safety`, `display_rules`
  only -- built-in source does not bypass the manifest here).
- All `tool_guide` and `domain_rules` extensions (built-in and plugin
  alike, after `applies_to` filtering) are rendered as a manifest
  section in the system prompt: one line per extension with `name` and
  the one-sentence `summary` from the resource manifest. Bodies are
  pulled on demand by the LLM via `get_guide`. The summary is the only
  signal the model uses to decide whether to fetch -- plugin authors
  (and built-in maintainers) should write summaries that make the
  guide's applicability obvious.
- No budget parameter: manifest entries are one-line summaries
  (~12 tokens each), naturally bounded by the number of registered
  extensions. If that bound ever proves too loose, a cap can be added
  without breaking the API.
- `get_guide` tool docstring tells the model to consult the manifest
  before calling tools whose guides are advertised there.
- Intended for Opus, Sonnet, and local models known to handle
  tool-call chaining well.

**Selection.** `settings.prompt_strategy` is a string with values
`"budgeted" | "manifest"`. Defaults to `"budgeted"` so the
current cost tier is the safe default. Operators flip it per deployment.
A future enhancement can drive this from `settings.llm_model` automatically.

### 4. Migration of `chat.py`

The mandatory rules in `chat.py:135-298` move to core as `base_rules`. They
also receive a deduplication pass during migration: rules 4, 7, 7B, 7C all
restate display rules that also appear inside `query_guide` and
`entity_guide`. The audit consolidates each rule to a single canonical
statement. The audit is in-scope for this ADR because it is the largest
single win available and is structurally entangled with the migration.

After migration, `chat.py:system_prompt()` simplifies to:

```python
def system_prompt(settings, session, selected: SelectedPrompt,
                  schema_context, ermrest_syntax) -> str:
    base = _base_persona(settings, session)
    parts = [base, selected.injected_text]
    if selected.manifest_entries:
        rendered = [f"- {e.name}: {e.summary}" for e in selected.manifest_entries]
        parts.append("Available on-demand guides (fetch via get_guide):\n" +
                     "\n".join(rendered))
    if schema_context:
        parts.append("Schema:\n" + schema_context)
    if ermrest_syntax:
        parts.append("ERMrest URL syntax reference:\n" + ermrest_syntax)
    return "\n\n".join(parts)
```

`_GUIDE_PROMPT_NAMES`, `_fetch_guides`, and the inline rule lists are
deleted.

---

## Consequences

### Enabled

- Plugins extend the system prompt without touching UI code.
- The same plugin works correctly under any model tier; injection policy is
  the operator's choice.
- Catalog-scoped guidance: deriva-ml content stays out of the prompt when
  no ML catalog is connected. Same for any other domain plugin.
- Token budgets are observable. A startup log enumerates every registered
  extension with its category and estimated cost; per-session logs show
  which were selected and which were dropped.
- The chat.py rules cleanup happens as a side effect rather than as a
  separate project nobody is going to schedule.

### Constraints accepted

- The manifest resource at `deriva://prompt-extensions` is a
  deriva-specific MCP resource URI. Acceptable: MCP resources are
  designed for server-defined content, and the resource is ignorable by
  other MCP clients.
- Plugins must use ASCII (per project convention) and stay within their
  declared estimated token budget. Violations are logged but not blocked.
- Plugin authors cannot mark their own content as "force always inject."
  That decision belongs to the operator via strategy choice. Plugins
  express intent through `category`, `priority`, and `applies_to` only.
- `max_tokens` on `BudgetedStrategy` caps plugin-contributed optional
  content only, not total optional content. Operators sizing the budget
  think about plugin overhead on top of a built-in baseline, not a
  single combined number. Built-in guides have an implicit cost the
  core team is responsible for keeping reasonable.
- Built-in `tool_guide` and `domain_rules` extensions always inject
  under the budgeted strategy. There is no per-extension operator
  override to disable a specific built-in. A deployment that needs to
  trim the built-in baseline must either run a custom build or wait
  for a future per-extension disable mechanism (out of scope here).
- No replacement mechanism: a plugin cannot stand in for a built-in
  guide. If both are registered they both inject (or both manifest).
  Real replacement is a separate feature, not addressed by this ADR.
- Two strategies cover the immediate need but the design is open to more
  (e.g. an adaptive strategy that promotes/demotes extensions based on
  observed tool-call patterns). Out of scope for this ADR.

### Tradeoffs

- One mega-prompt (under the budgeted strategy) loses per-section cache
  granularity -- any change to any extension invalidates the cache. The
  current implementation already has this property, so no regression. A
  future refinement could mark cache breakpoints per category.
- `get_guide` adds one tool to the catalog. Tool-list token cost grows by
  a fixed amount (~80 tokens). Acceptable.
- Plugins cannot inject content unconditionally. This is deliberate:
  unconditional injection is the path to the current bloat.

### Risks

- Plugins cannot structurally displace built-in content under the
  budgeted strategy: built-in `tool_guide` and `domain_rules` extensions
  inject unconditionally; only plugin-contributed optional content is
  budget-bound. The startup-time priority check (plugin extensions with
  `priority >= 90` are rejected with an ERROR log) closes the path by
  which a plugin could even attempt the displacement. The remaining
  risk is purely intra-plugin: one plugin crowding out another. Plugins
  dropping under budget pressure are logged at WARNING so operators can
  see when budget should be raised or plugin content trimmed.
- The manifest strategy depends on the model actually calling `get_guide`.
  If the chosen model is bad at it, the operator should switch to the
  budgeted strategy. Detected by transcript review or a metric on tool-call
  rates.

---

## Implementation phases

1. **PluginContext API and manifest resource.** Add
   `system_prompt_extension`, the `ExtensionDeclaration` dataclass
   (including the internal `source` field), the
   `deriva://prompt-extensions` resource handler, and the startup-time
   priority check that rejects plugin extensions with `priority >= 90`.
   Unit tests cover the round-trip from registration to resource fetch
   to body retrieval, plus the rejection of mis-prioritized plugin
   registrations.
2. **Migrate built-ins.** Convert the four existing guides and the chat.py
   rule block to extensions. Run the deduplication audit during this step.
3. **`get_guide` tool.** Register in `tools/prompts.py`; ensure its
   docstring is consistent with both strategies.
4. **UI discovery and strategy interface.** Replace `_GUIDE_PROMPT_NAMES`
   with a `deriva://prompt-extensions` resource fetch; introduce
   `PromptInjectionStrategy`, `BudgetedStrategy`, and
   `ManifestStrategy`. `settings.prompt_strategy` selects between them.
5. **Catalog scoping.** Implement `AppliesTo` with `catalog()` and
   `hostname_pattern()` constructors; wire `SessionContext` to the
   strategies.
6. **Plugin-authoring guide update.** Document the API, the metadata schema,
   the priority convention, and the two strategies. Provide a worked
   example for a hypothetical ML plugin.

Phases 1-4 are the minimum useful slice. Phase 6 is documentation, not
code.

Note on Phase 5: cross-deployment isolation is already handled at the
plugin-allowlist layer (`DERIVA_MCP_PLUGIN_ALLOWLIST`). FaceBase's
chatbot does not install `deriva-ml-mcp`, so ML content cannot reach
FaceBase sessions regardless of `applies_to`. Phase 5 covers the
*intra-deployment* case: a single chatbot that hosts multiple catalogs
with different needs, where a plugin is installed but should only
contribute to sessions whose connected catalog matches its scope.

---

## Open questions

- **Tool-side guide linkage.** The manifest strategy relies on the LLM
  reading each extension's `summary` to decide when to fetch the body.
  If summaries prove insufficient, explicit linkage should go tool-side:
  `@ctx.tool(mutates=False, guide="ml_workflow")`. Co-locating the
  linkage with the tool means adding a new tool naturally prompts the
  author to set its guide; a plugin tool can point at a built-in guide
  (cross-plugin linkage); there is one place to update when tools
  change. Manifest rendering inverts the relation. The inverse design
  -- a guide-side list of tools the guide covers -- drifts silently,
  can go incomplete, and cannot express the cross-plugin case. Add
  tool-side linkage only if measured evidence shows summaries alone are
  insufficient.
- **`prefer_on_demand` flag.** Plugin authors may eventually want to
  signal "this content is large reference material; never eager-inject
  me, even if my category and priority would otherwise qualify." A single
  boolean on `system_prompt_extension` (`prefer_on_demand: bool = False`)
  plus one branch in `BudgetedStrategy` (route to `on_demand_names`
  instead of packing into the eager block) would cover it. Deferred until
  a concrete plugin contribution needs it -- low priority and accurate
  `estimated_tokens` produce functionally similar behavior under budget
  pressure. Non-breaking to add later.
- Should `applies_to` evaluation run server-side or client-side? Current
  preference: client-side, since `applies_to` is limited to declarative
  forms (catalog match, hostname glob) and the UI already knows the
  current session. Revisit only if non-declarative forms are added.
- Should the manifest strategy include a token estimate per advertised
  guide so the model can weigh the cost? Possibly useful with smaller
  context windows; defer until measured.
- Per-extension cache breakpoints (Anthropic prompt cache `cache_control`
  on each section) is a possible future optimization. Skipped for now to
  keep the initial change focused.