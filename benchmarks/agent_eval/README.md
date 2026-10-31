# Agent-outcome benchmark (harness only)

`run.py` measures the context the tool would give an agent for a task. It does **not** run a
model: this environment has no model endpoint, and fabricating resolve rates would be worse
than shipping nothing.

- `--dry-run` (default): indexes the task repo, builds slices for the task's `symbols`, and
  writes payload/touched-file token counts plus `status: "UNMEASURED"`.
- `--execute`: exits with an explanatory error until a model adapter is added.

To make this a real benchmark, implement an adapter that, per task: checks out `base_commit`,
runs the agent with and without the MCP server, applies the patch, runs `test_command`, and
records resolve rate, tokens and tool calls. Results must be committed as raw logs.
