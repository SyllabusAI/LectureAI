**Syllabus {{version}} for Mac** (Apple silicon and Intel, macOS 13 or newer).

**What's new**

- Setup starts with signing in. Your Syllabus account covers transcription and summaries, so there are no API keys to find or paste. Before, Setup opened on two API key fields and put the account last.
- A one-line install that skips the "could not verify" warning. Paste this into Terminal and Syllabus downloads, installs, and opens on its own:

  ```
  curl -fsSL https://raw.githubusercontent.com/SyllabusAI/LectureAI/main/install.sh | bash
  ```

  Run the same line again any time to update. Step-by-step help is at [maincoursemedia.com/syllabus/install](https://maincoursemedia.com/syllabus/install/).

Download `{{dmg}}`, open it, and drag Syllabus to Applications. Open Syllabus from Applications and it starts on its Setup page: sign in to your Syllabus account, pick a microphone, enter your class schedule, and connect Google Drive. A signed-in Mac holds no API key of its own, because transcription and summaries are billed to the account. Nothing else to install.

If you download the disk image instead of using the one-line install: this build is not yet signed with an Apple Developer ID, so the first time you open it macOS says it could not verify the app and offers only Done or Move to Trash. Click Done, then open System Settings, choose Privacy & Security, scroll to the Security section, and click Open Anyway. Confirm once more and Syllabus opens. You do that once per version.

Already using Syllabus? Quit it from the menu bar, replace the copy in Applications with this one, and open it again. Your settings, schedule, recordings, and account stay where they are.
