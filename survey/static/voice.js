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
  '2W': ['two wheeler', 'twowheeler', 'bike', 'motorcycle', 'motorbike', 'scooter'],
  '3W_Auto': ['auto', 'three wheeler', 'autorickshaw', 'auto rickshaw'],
  // NOT bare "van" or "jeep" alone would be fine, but "car" is the one people say.
  Car_Jeep_Van: ['car', 'jeep', 'van', 'car jeep van'],
  // "LCV" spelled out is the single worst phrase for a recogniser in this list, so the
  // real-word alternatives matter more here than anywhere else.
  LCV: ['l c v', 'lcv', 'el see vee', 'elsie', 'pickup', 'pick up', 'light commercial',
        'tempo', 'goods carrier'],
  Mini_Bus: ['mini bus', 'minibus', 'small bus'],
  Bus: ['bus', 'coach'],
  Tractor: ['tractor'],
  Tractor_Trailer: ['tractor trailer', 'tractor with trailer', 'trailer'],
  '2Axle_Truck': ['two axle', 'two axle truck', 'lorry', 'truck', 'two axel'],
  '3Axle_Truck': ['three axle', 'three axle truck', 'three axel'],
  // "MAV" comes back as "I am way", "em ay vee", "may" depending on the speaker. These
  // are the mishearings actually observed; anything else is what training is for.
  MAV: ['m a v', 'mav', 'i am way', 'am way', 'em ay vee', 'multi axle',
        'multi axle vehicle', 'container', 'four axle'],
  Cycle: ['cycle', 'bicycle', 'push cycle'],
  Cycle_Rickshaw: ['cycle rickshaw', 'rickshaw'],
  // Bare "cart" is deliberately absent: it is one phoneme from "car", and confusing an
  // Animal_Cart (6.0 PCU) with a Car_Jeep_Van (1.0) is the most expensive mistake on
  // this list. Say the whole phrase.
  Animal_Cart: ['animal cart', 'bullock cart'],
  Other: ['other', 'something else'],
};

const CONTROL = {
  // "yes" is short and unstressed and comes back as "s", "yes." or nothing. The longer
  // synonyms are more reliable, which is why several are offered.
  __yes: ['yes', 'correct', 'right', 'confirm', 'okay', 'yep', 'yeah', 'same', 'agreed'],
  not_a_vehicle: ['not a vehicle', 'no vehicle', 'reject', 'not vehicle'],
  unclear: ['unclear', "can't tell", 'cant tell', 'cannot tell', 'not sure', 'dont know',
            "don't know", 'unsure'],
  __skip: ['skip', 'next', 'pass'],
  __back: ['back', 'previous', 'go back', 'undo'],
};

/* What the training screen suggests saying. Not simply the first phrase in the table:
   for MAV and LCV the first entry is the spelled-out form, which is precisely the one
   that mishears — telling somebody to say "m a v" while training would teach the app
   their worst pronunciation. Suggest the real word, keep the letters as fallbacks. */
const SUGGEST = {
  MAV: 'multi axle',
  LCV: 'pickup',
  '2Axle_Truck': 'lorry',
  '3Axle_Truck': 'three axle',
  Car_Jeep_Van: 'car',
  '3W_Auto': 'auto',
  Cycle_Rickshaw: 'cycle rickshaw',
  Animal_Cart: 'bullock cart',
};

/* Every command the training screen can teach, in the order it presents them. */
export const TRAINABLE = [
  { id: '__yes', label: 'Yes / correct', say: 'yes' },
  { id: '__skip', label: 'Skip', say: 'skip' },
  { id: '__back', label: 'Back', say: 'back' },
  { id: 'not_a_vehicle', label: 'Not a vehicle', say: 'not a vehicle' },
  { id: 'unclear', label: "Can't tell", say: "can't tell" },
  ...Object.keys(SPOKEN).map(c => ({
    id: c, label: c, say: SUGGEST[c] || SPOKEN[c][0], cls: true,
  })),
];

/* What this speaker's voice actually produces, learned on their machine.
   localStorage, not the server: it is one person's pronunciation on one laptop, and
   syncing it between surveyors would make everybody's recognition worse. */
const ALIAS_KEY = 'tl_voice_aliases';

export function loadAliases() {
  try { return JSON.parse(localStorage.getItem(ALIAS_KEY) || '{}'); }
  catch { return {}; }
}

export function saveAlias(phrase, id) {
  const p = norm(phrase);
  if (!p) return loadAliases();
  const a = loadAliases();
  a[p] = id;
  try { localStorage.setItem(ALIAS_KEY, JSON.stringify(a)); } catch { /* private mode */ }
  return a;
}

export function forgetAliases(id) {
  const a = loadAliases();
  for (const [p, v] of Object.entries(a)) if (!id || v === id) delete a[p];
  try { localStorage.setItem(ALIAS_KEY, JSON.stringify(a)); } catch { /* private mode */ }
  return a;
}

export function createVoice({ onAction, onHeard, onState, minConfidence = 0.6 }) {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) {
    return { supported: false, start() {}, stop() {}, toggle() {}, listening: () => false };
  }

  let rec = null, on = false, classes = [], attrs = [];
  let capture = null;      // set while the training screen is listening for one phrase

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
    /* Learned last, so a trained phrase wins over a built-in guess. If this speaker's
       "MAV" reliably comes back as "I am way", that mapping must beat anything the
       default table thinks those words mean. */
    for (const [phrase, id] of Object.entries(loadAliases())) {
      if (id in SPOKEN) map.set(phrase, { kind: 'class', value: id });
      else if (id in CONTROL) map.set(phrase, { kind: 'control', value: id });
      else if (attrs.some(a => a.key === id)) map.set(phrase, { kind: 'attr', value: id });
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
        /* Training intercepts before matching. A phrase being taught must never also be
           executed -- saying "MAV" to teach it and having it answer the vehicle behind
           the modal is how a training session corrupts a survey. */
        if (capture) {
          const alts = [];
          for (let a = 0; a < r.length; a++) {
            const t = norm(r[a].transcript);
            if (t && !alts.includes(t)) alts.push(t);
          }
          const cb = capture; capture = null;
          cb(alts);
          return;
        }
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

    /* Listen for one phrase and hand back every alternative the recogniser produced,
       without acting on any of them. Resolves to [] if nothing is heard in time. */
    captureNext(timeoutMs = 6000) {
      return new Promise(resolve => {
        const wasOn = on;
        if (!rec) build();
        let settled = false;
        const done = alts => {
          if (settled) return;
          settled = true;
          capture = null;
          if (!wasOn) { on = false; try { rec.stop(); } catch { /* not running */ } }
          resolve(alts || []);
        };
        capture = done;
        on = true;
        try { rec.start(); } catch { /* already running */ }
        setTimeout(() => done([]), timeoutMs);
      });
    },
  };
}
