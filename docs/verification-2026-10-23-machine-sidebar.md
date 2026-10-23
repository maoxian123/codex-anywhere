# Machine sidebar sections — 2026-10-23

## Behavior

The machine list uses separate equal-height sections for expanded nonempty machines. Their headers remain outside their own scroll containers. Each machine keeps its own scroll position, loaded conversations, pagination and project actions; a large inventory no longer pushes the next machine below the whole inventory. Machines can be collapsed independently without unmounting their list, and empty machines use a compact section. Opening a different conversation reveals its machine; background metadata refreshes preserve explicit collapse choices. Portaled conversation menus close when their machine scrolls or collapses.

## Validation

- Windows frontend suite: 214 passed. Tests include unbalanced machine inventories, independent scroll position preservation across updates, machine/project collapse independence, route reveal, menu dismissal and existing pagination/filter flows.
- TypeScript and Vite production build passed; Prettier and git diff --check passed.
- Independent review of component and CSS completed. Browser pixel QA was attempted against a disposable in-memory fixture, but this tool session had no available browser (iab unavailable); no screenshot or geometric verification is claimed. The temporary fixture was stopped.
- No Python or node runtime changes; the already verified node release remains untouched. Production deployment follows docs/server-deployment.md, with package/static asset and service health checks only.
