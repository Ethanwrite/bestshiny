# Agent runtime boundary

Director, assistant director, cinematographer, lighting, camera operator, prompt compiler and reviewer agents
use `ProductionEngine`, `GenerationGateway`, `MediaRegistry` and the Skill registry. Provider clients are not injected
into agents. Google Flow protocol details therefore remain isolated inside `GoogleFlowProvider`.

The creative entry point is `skills/director/SKILL.md`. After director approval, `skills/prompt-compiler/SKILL.md`
translates the locked shot specification into provider-specific instructions without changing story actions.
