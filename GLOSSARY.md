# Codex Mem

This glossary defines the terms used for project memory. It describes the meaning of each term, without defining its implementation.

## Language

**Episode**:
A bounded section of work with its own evidence and scope.
_Avoid_: Chat, session

**Claim**:
A statement about a requirement, decision, fact, task, or constraint.
_Avoid_: Observation, source

**Episode head**:
The current version of an episode.
_Avoid_: Latest chat, latest observation

**Episode consolidation**:
A new episode version that brings together separate episodes about the same explicitly identified task.
_Avoid_: Topic grouping, session merge

**Source custody**:
A record of whether retained user material has a supported representation or still requires reconciliation.
It does not establish an active requirement.
_Avoid_: Requirement acceptance, automatic obligation

**Interactive host**:
The application that runs the user's interactive agent and supplies memory events.
_Avoid_: Background runtime, model provider

**Background runtime**:
The agent runtime that executes a memory generation attempt from prepared input.
_Avoid_: Interactive host, model

**Model provider**:
The service or configured provider that supplies the model for background execution.
_Avoid_: Background runtime, interactive host

**Execution profile**:
A complete choice of background runtime, model provider, exact model, reasoning level, and timeout for an attempt.
_Avoid_: Interactive model, host settings

**Execution identity**:
The observed runtime, provider, model, reasoning level, and execution identifiers for a background call.
_Avoid_: Requested profile, configured model
