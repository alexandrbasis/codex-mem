# Preserve source content during output repair

Status: accepted. Date: 2026-10-29.

## Problem

A retained native review accepted details from earlier context as if a later message contained them. The citation pointed to a real source span, but that span did not contain the attributed price or duration. Structural citation validation could not detect this semantic error.

The observation job had already completed. Retrying that job or writing a replacement through explicit memory tools would bypass its lifecycle or review boundaries.

## Decision

Keep source content separate from contextual interpretation in generation, correction and support review. History can explain a reference. It cannot add content to a claim about a specific message. A combined account must retain the origin of each detail. The reviewer must derive its evidence assertion from the selected source text before comparing it with the candidate.

Use one shared support, original-report interpretation and outcome review sequence for ordinary processing and explicit repair. Profiles, deadlines, evidence bounds and the ordinary correction allowance stay unchanged.

Expose a maintenance operation for selected outputs of one completed observation attempt. Its guard binds the project, original job and attempt, original outputs, retained source content and current execution settings. Replacement text is a proposal and must pass the existing shape, support, outcome and configured optional semantic checks. The challenged outputs cannot serve as evidence for their replacements.

Recheck the guard inside the commit transaction. Create successor records with links to the raw sources and replaced outputs. Supersede only the selected derived records. Preserve original source rows, the completed job, its attempts and its failure receipts. An identical successful call returns its repair receipt without another model call. No model runs inside the commit transaction.

## Evidence and limits

An observed native comparison rejected the retained misattributed candidate after the contract change and accepted a version that kept the sources separate. This finite check does not prove general semantic accuracy. A valid citation still establishes ownership, not entailment or external truth.

The repair records its reviewer identities, diagnostics and metrics separately from the original observation attempt. Failed repair reviews return evidence to the maintenance caller without changing the database; that caller must retain the receipt. Repair does not rewrite the original attempt's usage history or automatically audit other stored records.

The maintenance operation does not enable projects, change provider settings, reopen the queue job or authorize separate user-memory authoring.
