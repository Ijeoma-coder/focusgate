# FocusGate

Games stay locked until you prove you did the work.

You say what you are going to do, FocusGate checks that you did it (by watching
your screen, watching you through the webcam, or judging a timelapse video you
upload), and verified focus time
turns into game time you can spend.

## Set it up (Windows, about 5 minutes)

1. Install Python 3.10 or newer from python.org. On the first installer screen,
   tick "Add python.exe to PATH".
2. Put this folder somewhere permanent, for example `C:\Users\you\FocusGate`.
   Autostart points at this location, so do not move it afterwards.
3. Double-click `setup.bat`. It creates a private environment in `.venv` and
   installs the libraries.
4. Double-click `start.bat`. A window opens and a green padlock appears in the
   system tray. Closing the window hides it to the tray.

If `start.bat` seems to do nothing, run `debug.bat` to see the error.

## First run

1. **Blocklist tab.** The common launchers (Steam, Epic, Riot, Battle.net and
   so on) are already listed. Add your own games with "Add a game (.exe)", or
   open the game and use "Add a running program". "Add a folder" blocks every
   program inside it, which suits a Steam library folder.
2. **Settings tab.** Pick an AI backend, paste your API key, press "Save key",
   then "Test".
   - Gemini (free tier): create a key at aistudio.google.com. If the test says
     the model was not found, replace the model name with a Flash-Lite model
     that your AI Studio account lists.
   - Claude: create a key at console.anthropic.com and add credit.
   - Rules only: no key, nothing leaves the laptop, but timelapse proof is
     unavailable and unfamiliar windows stay unverified.
3. **Camera tab.** Press "Test camera" to check that your face is in frame.
   Windows may ask for camera permission the first time.
4. **Focus tab.** Type a task, pick how long, pick which apps count, pick a
   kind of proof, and start.

## How a session works

**Watch my screen.** Every 5 seconds FocusGate reads the active window's
program and title.

- A window on the "never on-task" list counts as off task.
- A program or title word from the chosen on-task list counts as on task.
- Anything else is screenshotted once and sent to the AI. The verdict is
  remembered for that window title, and time already spent there is credited
  or not after the fact.
- About every 10 minutes a random screenshot check confirms that an allowed
  app really shows work.
- No keyboard or mouse input for 10 minutes pauses the clock.

**Watch me on camera.** Every 5 seconds FocusGate takes one webcam frame and
checks it on the laptop.

- A face in the frame counts as present, and the time counts.
- With your head down reading or writing, the camera may lose your face.
  Movement keeps counting for 10 minutes after your face was last seen (the
  "head-down allowance"). After that the time stays unverified until you
  glance up.
- A still scene for 20 seconds means nobody is there, and the clock pauses.
- An obvious distraction in the active window (for example Netflix) still
  counts as off task.
- Optional, off by default: about every 5 minutes one frame goes to the AI to
  check that you look like you are working, for example not on your phone. A
  positive answer also renews the head-down allowance.

The camera proves you are at the desk for the whole session. It cannot see
what you are working on.

**Screen and camera together.** The strongest proof. The screen rules decide
whether the work is on task, and the camera decides whether you are there.
While the camera sees you, the no-input pause is switched off, so watching a
long lecture with your hands off the keyboard still counts.

**Timelapse video.** Start the session, film your desk or screen as a timelapse
while you work, then move the video to the laptop and upload it. FocusGate
samples 16 frames and the AI judges each one. At least 60% must look on-task.
It rejects videos saved before the session started, videos already used, and
videos where nothing moves.

**Earning and spending.** By default one focus hour earns 30 game minutes.
"Start game time" spends banked minutes on a countdown. Games open freely until
it ends, with a warning 2 minutes before. Ending early refunds the rest.

## Strict mode

Off by default so you can set things up freely. When you turn it on:

- Removing a game from the blocklist, earning faster, shortening the cooldown,
  and turning strict mode off all wait for the cooldown (12 hours by default).
- A small watchdog process restarts FocusGate if it is closed.
- Quit disappears from the tray menu.
- If FocusGate is killed while the PC stays on, the gap shows up in History.

## Stopping it

`stop.bat` closes FocusGate and the watchdog cleanly, even in strict mode. It
is logged in History. To remove FocusGate completely, untick "Start FocusGate
when I sign in" in Settings, run `stop.bat`, then delete the folder and
`%APPDATA%\FocusGate`.

You are the administrator of your own laptop, so this tool adds friction
rather than a real barrier. It works because bypassing it takes a deliberate,
visible step.

## Privacy

- Camera frames are checked on the laptop and never saved to disk. Nothing
  from the camera leaves the laptop unless you turn on the AI camera check in
  the Camera tab. With it on, pictures of you and your room go to the chosen
  AI backend, so read the next point first.
- The camera light stays on for the whole camera session.
- Screenshots are shrunk to 1024 pixels wide, sent only when the rules cannot
  decide (plus the spot checks), and never saved to disk.
- Google states that Gemini free-tier requests may be used to improve its
  products. Use the Claude backend or rules-only mode if that matters to you.
- API keys are stored in Windows Credential Manager, not in the config file.
- Settings and history live in `%APPDATA%\FocusGate`.

## Known limits

- A game or launcher that runs as administrator cannot be closed by a normal
  program. FocusGate tells you when this happens. Running `start.bat` as
  administrator fixes it.
- Camera presence is fooled by anything that moves (a fan, a TV behind you)
  for up to the head-down allowance, and a second person in frame counts as a
  face. The AI camera check closes most of that gap.
- Face detection needs reasonable light. Test it in the room and at the time
  of day you normally work.
- The main screen is the one captured. A second monitor is not seen.
- Watching a long lecture without touching the mouse will trip the idle pause.
  Raise the idle limit in Settings if that is how you study.
- The Windows-specific parts (closing programs, reading the active window,
  screen capture, opening the webcam, tray icon, autostart) were written carefully but could not be
  run on Windows before delivery. Everything else is covered by the tests.

## For developers

```
focusgate/
  app.py        window and tray icon
  blocker.py    closes blocklisted programs
  monitor.py    live session tracking (screen, camera, or both)
  camera.py     webcam presence: face detection and movement
  models/       bundled YuNet face-detection model
  timelapse.py  video sampling and checks
  judge.py      Gemini and Claude backends
  rules.py      free window classification
  config.py     settings, blocklist safety, cooldowns
  store.py      SQLite: bank, sessions, events
  watchdog.py   strict-mode restart process
  winutil.py    Windows API helpers
tests/test_core.py
```

Run the tests with `.venv\Scripts\python.exe tests\test_core.py`.

## Credits

Face detection uses the YuNet model by Shiqi Yu and contributors
(github.com/ShiqiYu/libfacedetection.train), BSD 3-Clause. The licence is in
`focusgate/models/LICENSE-yunet.txt`.
