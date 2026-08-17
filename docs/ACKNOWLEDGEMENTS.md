# Acknowledgements

## MetaSwarm

Régie's 2026 workflow expansion was materially inspired by
[MetaSwarm](https://github.com/dsifry/metaswarm), created by Dave Sifry and
developed with its contributors. MetaSwarm is distributed under the MIT License.

The concepts that directly influenced Régie's direction include:

- independent feasibility, completeness, and scope/alignment plan reviews;
- risk-specific architecture, security, API, migration, and UX review lenses;
- fresh adversarial reviewers with evidence-backed contract verdicts;
- planned human checkpoints at risky or externally dependent boundaries;
- dependency-aware parallel work units and a final cross-unit review;
- selective project-knowledge priming and post-run reflection;
- guided repository setup, provider health checks, cost controls, and PR
  shepherding;
- durable handoff and recovery artifacts.

Régie does not use MetaSwarm as its execution substrate. These ideas are
implemented independently around Régie's differentiating choices: an explicit
Python state machine owns every transition; command exit codes and diff checks
are authoritative; test and implementation authors are mechanically separated;
git operations belong to the orchestrator; provider outputs follow strict
schemas; and transcripts/state live outside the target repository.

We are grateful to MetaSwarm for making a rigorous, production-minded agentic
software-development workflow public and inspectable.

## Superpowers

Régie's original design and planning documents also use the spec-driven
brainstorming and implementation-planning practices popularized by
[Superpowers](https://github.com/obra/superpowers). Those influences remain
visible under `docs/superpowers/`.
