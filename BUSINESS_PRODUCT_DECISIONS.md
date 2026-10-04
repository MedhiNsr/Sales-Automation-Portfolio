# Business & Product Decisions

## Make the prospect registry central
A prospect may exist in several lists at once. The central registry owns the decision about whether a prospect can enter or continue a campaign.

## Preserve human control over lead selection
The prospect list is reviewed before automation so account ownership, exclusions and relationship context remain human-controlled.

## Prevent cross-campaign collisions
Campaign activity, exclusions, replies, meeting states and cooldowns can affect eligibility. A new campaign checks shared prospect state instead of assuming its CSV is authoritative.

## Separate validation from execution
A validated campaign can be converted into an executable email campaign, creating a boundary between commercial preparation and outbound execution.

## Model outreach as state transitions
Campaigns have due steps and prospects have states. Persisting those states makes follow-up logic and collision prevention possible.

## Be conservative around uncertain sends
Duplicate outreach damages trust. Where send state is uncertain, the safer behavior is reconciliation or human review rather than a blind retry.

## Automate repetition, not relationship judgment
Timing, tracking, personalization plumbing and repetitive execution can be automated. Who should be contacted remains a commercial decision.

## AI-assisted development
The prototype was iteratively developed with AI coding assistance. My role centered on the business problem, workflow, operating rules, edge cases, testing and product decisions.
