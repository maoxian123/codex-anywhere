# Compact conversation sidebar — 2026-10-23

## Behavior

- Conversation rows display one title line, without a date/time subtitle. Important
  running, waiting, error, stopped and loading states remain inline; full titles
  and states are available on hover. Pinned and selected conversations remain clear.
- Desktop conversation rows have a 32px minimum height instead of 50px, with the
  previous bottom margin removed. Project headers use one line with name/count;
  full directories remain in hover titles and accessible labels. Project minimum
  height is 30px and machine headers use 32px.
- Four view filters share one row. New-conversation/search controls, scope actions,
  footer spacing and machine/project gaps are compact. Unselected empty projects
  retain their filter/new-conversation actions without a repeated empty hint.
- Machine sections still share space and scroll/collapse independently. Menus and
  project actions appear on hover/focus/open for mouse users and remain visible on
  touch devices. Narrow screens and coarse pointers retain 44px action targets.

## Validation

- Windows frontend suite: **231 passed**. Coverage includes title-only rows,
  accessible full titles and directories, seven non-idle states, empty-project
  density, keyboard/menu actions, filtering, multi-machine scrolling/collapse and
  pagination, as well as the existing composer/queue and application regressions.
- Final TypeScript/Vite production build, Prettier and `git diff --check` passed.
- CSS cascade reviewed against the existing base/mobile styles. Minimum heights
  above describe the CSS; no real-browser geometry or screenshot check is claimed
  because this tool session has no available browser.
- No Python, node runtime, API contract or dependency changes; the Python suite was
  not rerun for this presentation-only change. Deployment targets the existing Linux
  Hub; production checks cover installed package bytes, service health, database
  revision and static assets only. No production tests or inference are run.
