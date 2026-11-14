# CIDER External Baseline Reimplementations

These implementations reproduce the core scheduling ideas of representative
systems under CIDER's unified event-driven simulator.

They are not the original authors' implementations.

All methods share exactly the same:

- container requests;
- placement;
- initial layer state;
- layer sizes;
- link capacities;
- receiver bandwidth;
- peer upload bandwidth;
- reusable storage capacity;
- max-min fair bandwidth allocator.

## Registry

Centralized registry pulling only.

## Dragonfly-style

Idea-level abstraction of Dragonfly:

- peer-first delivery;
- load-aware parent selection;
- back-to-source fallback;
- in-flight duplicate suppression.

It operates at layer granularity in our simulator rather than Dragonfly's
native piece granularity.

## PeerSync-style

Idea-level abstraction of PeerSync:

- network-aware peer selection;
- local-network preference when network-domain information is available;
- content-popularity awareness;
- replica- and popularity-aware space reclamation;
- registry fallback when no suitable peer exists.

The current simulator does not reproduce PeerSync's DHT discovery latency,
block segmentation, Merkle verification, or tracker election.

## MetaPipe-Reactive-style

Reproduces the post-arrival reinforcement layer re-scheduling idea.

Proactive prefetching is intentionally disabled so that all compared methods
start from exactly the same layer state.

## ILR-SA-style

Reproduces image-layer reuse and sequential-arrangement ideas:

- layers with greater local reuse opportunity are scheduled earlier;
- future local reuse is considered during retention.

It does not reproduce unrelated placement decisions.
