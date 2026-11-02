# BlindPilot 0.52.7

Your old conversations can be read the way a document is read, and a conversation BlindPilot cannot read now says so.

- **A conversation in a web view:** Ctrl+Enter (or Shift+Enter) in Recent Conversations or Hermes Conversations reads the selected conversation in an accessible web view. The conversation title is the one heading, each exchange is a heading under it, so you can navigate by heading instead of hearing one long line. The reading cursor goes into the conversation when it opens, and Escape closes it from inside the conversation. Nothing a conversation names is fetched: a picture in an answer becomes a link, and a link opens in your browser only when you ask for it. Enter still opens the conversation in a new tab, the way the Open button does, and so does a double-click.
- **Every folder, by default:** Recent Conversations lists conversations from every folder, and remembers a narrower choice if you make one. A conversation is filed under the directory it ran in, and that directory is not always the one BlindPilot was started from, so scoping the list to one folder hid conversations you knew you had.
- **History source status:** when a conversation store cannot be read - locked by another program, damaged, or of a version BlindPilot does not know - Recent Conversations says so instead of showing an empty list, names the program and the problem, and says what to do about it. The explanation sits in a box called History source status, carries no path from your disk, and the conversations that could be read are still listed. Reload reads the stores again.

# BlindPilot 0.52.6

A performance fix for agent mode on Windows, from a CPU audit. Two background loops ran far more often than they needed to:

- When an agent runs in a hidden terminal, a watcher keeps any console window off your screen. For the first 8 seconds of each run it checked 200 times a second, and every check enumerated every window on the desktop. It now checks 40 times a second — still quick enough to banish a console in the frame it appears — which ends the burst of CPU at the start of every run.
- The terminal's output reader polled 100 times a second for the whole life of a session, including the long stretches where an agent is thinking or waiting for you. After a second of quiet it now backs off to 20 polls a second; output is still picked up within 50 ms when it arrives.

Chat mode was audited too and measured clean: no measurable CPU while idle, and streamed replies are parsed incrementally instead of being re-parsed as they grow.

# BlindPilot 0.52.5

This replaces 0.52.4, which was published and pulled within the hour: it shipped while the full test suite was red (type errors in the new code). Same features, gate green this time.

Four features from the latest The Chat Place (the app formerly called AIChat, now at 0.3.3), audited against BlindPilot and added where they fit its tab-based GUI.

- **Chat Permissions:** Conversation, Find and Browse, then Chat Permissions shows what this conversation's agent may do without asking — the approvals you granted for this session, remembered by BlindPilot, plus the allow, ask and deny rules and extra working folders from Claude's own settings files, each line naming the file it came from. It is an honest inventory, not an editor: session grants live inside the agent's own process, and BlindPilot never writes to Claude's settings files.
- **Question previews:** when Claude asks a question and an option carries a preview (a code block, a layout), the question dialog now shows it in a read-only Preview box that follows the option you pick.
- **Ask before each hand-off:** agent conversations gain a checkbox, "Ask me before each hand-off". With it on, a finished reply waits instead of being delivered, and Pass On the Waiting Reply in Find and Browse sends it to the other session — after editing it, if you want.
- **Copy links as Markdown:** links collected from a response can now be copied as Markdown, so pasting gives the link's words over its address instead of a bare URL. Properties also reports whether Remote Control is on for a Claude conversation.

The rest of The Chat Place's recent work is its single window holding every session at once, sorted into needs-you, working and idle. BlindPilot covers the same ground with tabs, Recent Conversations and its background notifications, so that design stays theirs.
