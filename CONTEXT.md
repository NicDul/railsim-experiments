# Capacity and flexibility

This context defines capacity and ordering terms used to compare rail service through a building block.

## Language

**Train class**:
Train instances that are interchangeable for ordering purposes. Differences in flow or direction that change movement define distinct classes.

**Entry ordering**:
The cyclic sequence of train classes entering a building block at each of its entry points within a repeating period; with several entry points, the combination of their sequences. Cyclic rotations are the same ordering; platform choice, overtaking and exit order inside the block are not part of it.
_Avoid_: Ordering profile, resource order, timetable

**Flexibility**:
The proportion of distinct entry orderings at a given mode, building block and volume for which a simulator-validated timetable exists. Unresolved orderings widen it into bounds; a sampled flexibility carries a confidence interval.

**Conditional no**:
An entry ordering excluded by a necessary check or solver infeasibility under the fixed measured-profile assumptions; it is not a proof about every simulator trajectory.

**Proven capacity**:
The highest volume supported by a validated timetable and a conclusive exclusion of the next volume under stated assumptions. It is not necessarily an unrestricted physical maximum; the evidence scope is part of the claim.
