# Development rules

Read TECHNICAL_SPECIFICATION.md before changes. Keep the agent loop generic:
the model selects actions and the harness owns execution boundaries, persistence
and budgets. Update that specification with implementation, deviations and test
results. Do not commit runtime data, local credentials or browser profiles.
