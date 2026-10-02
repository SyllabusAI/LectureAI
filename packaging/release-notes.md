**Syllabus {{version}} for Mac** (Apple silicon, macOS 13 or newer).

**What's new**

- Recording starts right away. Syllabus no longer asks you to tick a permission box before your first lecture. Getting permission to record is still up to you, as the [Terms](https://syllabusaccounts.maincoursemedia.com/terms) explain.
- A safer panel. Syllabus now loads only its own scripts and styles and refuses to be embedded in other pages, so a stray web page cannot tamper with it.
- Switching accounts on one Mac no longer carries the first account's Google Drive access over to the next one.
- The study assistant no longer keeps a copy of your lecture text on disk. What it fetches stays in memory for up to 30 minutes and is cleared when you quit or sign out. Copies from earlier versions are removed the first time you open it.
- Summaries and the study assistant treat lecture text as material to work from, never as instructions, so a line spoken in a lecture cannot steer them.
- Class times go in 15 minute steps. A class that starts at 9:30 or 10:45 can be entered at its real time on the Setup page instead of the nearest hour, and two classes that start within the same hour are filed to the right course.
- The Setup page now reminds you to keep your MacBook open while a lecture records. Closing the lid puts the Mac to sleep and ends the recording, so press Stop first. A dimmed or sleeping screen is fine, because Syllabus keeps the Mac awake while it records.
- A short guided tour the first time you open the dashboard. It walks through recording, your week, recent lectures, and the study assistant one card at a time, then points you to the student guide. Click Tour in the sidebar to see it again.
- The watcher turns itself off once your lectures are processed. Start it before class and it waits for the lecture, files the notes, then stops on its own instead of running in the background until you remember it.
- A course you pick by hand now covers just that recording. Afterward the picker goes back to your schedule, so the next class is filed under its own course. While your pick differs from the scheduled class, the dashboard says which course the lecture will be filed under.
- A lecture filed under the wrong course can be moved. Choose Move on it in Recent lectures, or run `syllabus refile`. Its summary and transcript move to the right course folder in Drive, keep their links, and the dashboard follows.

Download `{{dmg}}`, open it, and drag Syllabus to Applications. Open Syllabus from Applications and it starts on its Setup page: sign in to your Syllabus account, pick a microphone, enter your class schedule, and connect Google Drive. A signed-in Mac holds no API key of its own, because transcription and summaries are billed to the account. Without an account, paste two keys of your own instead, one from OpenAI and one from Anthropic. Nothing else to install.

This build is not yet signed with an Apple Developer ID, so the first time you open it macOS says it could not verify the app and offers only Done or Move to Trash. Click Done, then open System Settings, choose Privacy & Security, scroll to the Security section, and click Open Anyway. Confirm once more and Syllabus opens. You do that once per version.

Already using Syllabus? Quit it from the menu bar, replace the copy in Applications with this one, and open it again. Your settings, schedule, recordings, and account stay where they are.
