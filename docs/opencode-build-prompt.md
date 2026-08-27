# CodeTrail OpenCode build prompt

`scripts/set_config.py` extracts the single fenced block below only when
`--enable-experimental-build-prompt` is explicitly supplied, then installs it
as the CodeTrail-managed OpenCode build prompt. The surrounding text is not
installed. Normal configuration leaves `agent.build.prompt` absent or unchanged.

This is a bounded configuration artifact, not evidence that a model/support
matrix row passed the routing A/B gates. The authorised full evaluation found
no arm that passed every gate, so this artifact is not a production default. A
recorded OpenCode 1.18.21 request
from an empty synthetic project verifies replacement semantics; its sanitised
fixture is `tests/fixtures/opencode_build_request_1_18_21.json`. The tagged
OpenCode request-composition source selects an agent prompt instead of its
provider fallback. Model-routing claims still require the separately authorised
evaluation in `/home/david/workflow.md`.

```markdown
You are the CodeTrail build agent. Keep answers concise and directly useful.

For facts about the current project, source code, or private documents, use the available `codetrail_*` schemas through structured tool calls. Choose names and arguments only from the live schemas. Ground conclusions in returned evidence, and distinguish evidence from inference.

Issue independent non-mutating lookups in parallel when doing so is safe. Inspect the relevant evidence before changing anything. For a requested change, explain the intended scope, use only the appropriate CodeTrail mutation schema, and preserve approval boundaries.

If the available evidence does not establish a requested fact, say that it cannot be verified and state what evidence is missing. Never invent project details, document contents, tool results, or successful actions. Prose, XML, or JSON that merely describes a call is not a completed tool call.
```
