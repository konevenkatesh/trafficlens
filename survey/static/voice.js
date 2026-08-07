/* Hands-free reviewing.

   The reviewer's eyes are on the crop and their hand is on the mouse. Saying "lorry" is
   faster than finding the ninth button, and it keeps the eyes where the evidence is.

   Three rules, because a misheard word writes a wrong number into a report:

   **A command must be the whole utterance, not a word inside it.** "Not a car, that's a
   bus" contains "car"; acting on the first class name heard would record Car_Jeep_Van.
   So the transcript is matched whole, against a fixed vocabulary, and anything that is
   not an exact phrase is shown and ignored.

   **Low confidence is silence.** Chrome reports a confidence per result. Below the floor
   the app says what it thought it heard and does nothing, which is recoverable; guessing
   is not.

   **Every voice answer is visible and undoable.** The heard phrase is shown, and Back
   re-opens the vehicle — re-answering overwrites, so a wrong call costs one keystroke.

   Note on privacy, because it is not obvious: Chrome and Edge implement this by sending
   audio to the browser vendor's servers. It needs an internet connection and the speech
   leaves the machine. Nothing about the footage does — only the operator's voice saying
   vehicle names — but it is off by default and the UI says so. */

const norm = s => String(s || '').toLowerCase()
  .replace(/[.,!?;:]/g, ' ').replace(/\s+/g, ' ').trim();

/* Spoken forms for each MoRTH class. Indian English road vocabulary, not the label text:
   nobody says "Car_Jeep_Van", they say "car". Every phrase here has to be unambiguous
   across the whole table -- a phrase that maps to two classes is a coin toss. */
const SPOKEN = {
  '2W': ['two wheeler', 'twowheeler', 'bike', 'motorcycle', 'motorbike', 'scooter', 'two v'],
  '3W_Auto': ['auto', 'three wheeler', 'autorickshaw', 'auto rickshaw', 'tuk tuk'],
  Car_Jeep_Van: ['car', 'jeep', 'van', 'car jeep van'],
  LCV: ['l c v', 'lcv', 'el see vee', 'pickup', 'pick up', 'light commercial',
        'tempo', 'goods auto'],
  Mini_Bus: ['mini bus', 'minibus', 'small bus'],
  Bus: ['bus', 'coach'],
  Tractor: ['tractor'],
  Tractor_Trailer: ['tractor trailer', 'tractor with trailer', 'trailer'],
  '2Axle_Truck': ['two axle', 'two axle truck', 'lorry', 'truck', 'two axel'],
  '3Axle_Truck': ['three axle', 'three axle truck', 'three axel'],
  MAV: ['m a v', 'mav', 'multi axle', 'multi axle vehicle', 'container', 'four axle'],
  Cycle: ['cycle', 'bicycle', 'push cycle'],
  Cycle_Rickshaw: ['cycle rickshaw', 'rickshaw'],
  Animal_Cart: ['animal cart', 'bullock cart', 'cart'],
  Other: ['other', 'something else'],
};

const CONTROL = {
  __yes: ['yes', 'correct', 'right', 'confirm', 'ok', 'okay', 'yep', 'yeah', 'same'],
  not_a_vehicle: ['not a vehicle', 'no vehicle', 'reject', 'nothing', 'not vehicle'],
  unclear: ['unclear', "can't tell", 'cant tell', 'cannot tell', 'not sure', 'dont know',
            "don't know", 'unsure'],
  __skip: ['skip', 'next', 'pass'],
  __back: ['back', 'previous', 'go back', 'undo'],
};

export function createVoice({ onAction, onHeard, onState, minConfidence = 0.6 }) {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) {
    return { supported: false, start() {}, stop() {}, toggle() {}, listening: () => false };
  }

  let rec = null, on = false, classes = [], attrs = [];

  /* Rebuilt per vehicle: the attribute phrase depends on which attribute applies, and
     "government bus" must not be a live command on a car. */
  function vocab() {
    const map = new Map();
    for (const [cls, words] of Object.entries(SPOKEN)) {
      if (!classes.includes(cls)) continue;      // only classes this build actually has
      for (const w of words) map.set(w, { kind: 'class', value: cls });
    }
    for (const [act, words] of Object.entries(CONTROL)) {
      for (const w of words) map.set(w, { kind: 'control', value: act });
    }
    for (const a of attrs) {
      for (const w of (a.spoken || [norm(a.label)])) {
        map.set(norm(w), { kind: 'attr', value: a.key });
      }
    }
    return map;
  }

  function handle(transcript, confidence) {
    const t = norm(transcript);
    if (!t) return;
    const map = vocab();
    const hit = map.get(t);
    if (!hit) {
      // Shown, never guessed at. A reviewer who sees "heard: bus stop" learns to say
      // "bus"; one whose stray sentence silently recorded something does not.
      onHeard && onHeard(transcript, 'unknown');
      return;
    }
    if (confidence != null && confidence < minConfidence) {
      onHeard && onHeard(transcript, 'unsure');
      return;
    }
    onHeard && onHeard(transcript, 'ok');
    onAction && onAction(hit);
  }

  function build() {
    rec = new Rec();
    rec.continuous = true;
    rec.interimResults = false;
    rec.lang = 'en-IN';
    rec.maxAlternatives = 3;

    rec.onresult = ev => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (!r.isFinal) continue;
        // Try the alternatives too: "LCV" is often best-guessed as "elsie" with the real
        // phrase sitting second. First alternative that is a known command wins.
        let done = false;
        for (let a = 0; a < r.length && !done; a++) {
          const t = norm(r[a].transcript);
          if (vocab().has(t)) { handle(r[a].transcript, r[a].confidence); done = true; }
        }
        if (!done) handle(r[0].transcript, r[0].confidence);
      }
    };
    // Chrome ends the session after a stretch of silence. Restart, or the mic quietly
    // dies mid-session and the reviewer keeps talking to nothing.
    rec.onend = () => { if (on) { try { rec.start(); } catch { /* already starting */ } } };
    rec.onerror = ev => {
      if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
        on = false;
        onState && onState(false, 'microphone blocked — allow it in the browser');
      } else if (ev.error === 'network') {
        on = false;
        onState && onState(false, 'speech needs an internet connection');
      }
    };
  }

  return {
    supported: true,
    listening: () => on,
    setContext(cls, attributes) { classes = cls || []; attrs = attributes || []; },
    start() {
      if (on) return;
      if (!rec) build();
      on = true;
      try { rec.start(); } catch { /* already running */ }
      onState && onState(true);
    },
    stop() {
      on = false;
      try { rec && rec.stop(); } catch { /* not running */ }
      onState && onState(false);
    },
    toggle() { this.listening() ? this.stop() : this.start(); },
  };
}
