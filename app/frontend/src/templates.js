// Day-one onboarding content.
//
// SELFMOD_TEMPLATES — one-click starting points for the Self-Modify tab. Clicking one
// fills the request box; the user reviews and submits. They demonstrate the headline
// promise: describe what you want and the app rewrites itself into it.
//
// RUN_STARTERS — example prompts for the Run tab's empty state, showing off the agent's
// built-in capabilities (web, knowledge, harness introspection, the dev sandbox).

export const SELFMOD_TEMPLATES = [
  {
    title: "Rebrand the app",
    desc: "Change the accent color and header title.",
    prompt:
      "Change the app's accent color to violet (#7c3aed) and rename the header title " +
      "from 'Quine' to 'My Workspace'. Keep everything else working.",
  },
  {
    title: "Add a Tasks board",
    desc: "A simple Kanban board tab.",
    prompt:
      "Add a new 'Tasks' tab with a simple Kanban board (columns: To do, Doing, Done). " +
      "Cards can be added, moved between columns, and deleted, and they persist under the " +
      "data partition (QUINE_DATA_DIR) so they survive reboots.",
  },
  {
    title: "Add a Bookmarks tab",
    desc: "Save and open links.",
    prompt:
      "Add a 'Bookmarks' tab where I can save URLs with a title and open them in a new " +
      "tab. Persist them under the data partition so they survive reboots.",
  },
  {
    title: "Daily standup tool",
    desc: "Summarize recent harness activity.",
    prompt:
      "Add a Run-agent tool called 'standup_report' that reads the recent audit log and " +
      "produces a short daily-standup summary of what the harness did (commits, reboots, " +
      "rollbacks). Register it in the tools registry.",
  },
  {
    title: "CSV viewer",
    desc: "Paste CSV, see a sortable table.",
    prompt:
      "Add a 'CSV' tab where I can paste CSV text and view it as a sortable, searchable " +
      "HTML table. Pure frontend — no backend needed.",
  },
  {
    title: "Polish the Run tab",
    desc: "Nicer empty state + suggestions.",
    prompt:
      "Improve the Run tab's empty state with a friendlier welcome message and a few " +
      "example prompt buttons that fill the composer when clicked.",
  },
];

export const RUN_STARTERS = [
  "Search the web for the latest news on AI agents and summarize it with sources.",
  "What changed in the most recent version of this app?",
  "Search my uploaded documents and summarize what they say about pricing.",
  "Build a small Python script in the sandbox that fetches and prints today's date.",
];
