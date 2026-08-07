"""What fine-grained questions we ask about a vehicle, in one place.

The detector answers "what kind of vehicle is this" at the MoRTH level and stops. Several
things the survey actually has to report live below that level:

  * which axle class a heavy truck is -- 2Axle / 3Axle / MAV are three separate columns
  * whether a bus is APSRTC or private
  * whether a three-wheeler is a 7-seater maxi or an ordinary auto
  * whether a car is a commercial taxi

Each is the same shape of problem: take a crop of an already-detected vehicle of some
parent class, and answer one small closed question about it. So they get one mechanism
rather than four, and this file is the mechanism's data.

**Two kinds, and the difference matters.** `kind="class"` means the answer *replaces* the
MoRTH class -- a truck labelled 2Axle that turns out to have three wheel groups becomes a
3Axle_Truck, and the count moves between columns. `kind="attribute"` means the answer
*annotates* it -- an APSRTC bus is still a Bus in the vehicle count, and the operator only
decides which proforma column it also lands in. Conflating those would either lose buses
from the total or leave the axle columns uncorrectable.

**Crop policy is per question, because the evidence lives in different places.** Axles are
only countable side-on, so the widest box in the track wins. Livery and number-plate colour
are readable from any angle but need pixels, so the largest box wins. Getting this wrong is
not a tuning detail: a head-on truck at twice the pixel height shows exactly one axle.

**`mode` says who answers the question in production.** `"model"` means a trained
classifier decides and a person only spot-checks. `"human"` means a person decides every
one, and no model is trained at all. `"off"` means the question is not worth asking here
at all -- measured, not assumed -- and nothing downstream should surface it. That is not a fallback for attributes we have not got
to yet -- it is the right answer whenever the interesting class is rare. A yellow-board
taxi appears a handful of times in 1.5 hours of footage; a classifier fitted to that learns
"always private", scores 98% accuracy and never once finds a taxi. Rarity that defeats
training also makes the human cost trivial, which is what makes the trade a good one: a
handful of buses an hour is a few seconds of somebody's attention, and it is correct.

**`min_labels` is a refusal threshold, not a target.** Below it the trainer declines and
says how many more are needed, because a classifier trained on 40 examples of a 4-way
problem will report a confident number that means nothing -- which is the exact failure
this whole subsystem exists to undo.
"""

# Frame-choice policies. Named here so the labeller, the trainer and inference cannot
# disagree about which picture the question was answered from.
FRAME_WIDEST = "widest"     # most side-on: for anything read from the vehicle's profile
FRAME_LARGEST = "largest"   # most pixels: for anything read from livery, plates, signage

ATTRIBUTES = {
    "axles": {
        "kind": "class",
        "mode": "model",       # 471 labels, ResNet-18 @320px beats every VLM tested
        "label": "Axle count",
        "parents": ["2Axle_Truck", "3Axle_Truck", "MAV"],
        "values": ["2_axle", "3_axle", "4_or_more_axle", "not_a_truck", "unclear"],
        # The answer, mapped back to a MoRTH class. `not_a_truck` is -1: it means the
        # detection should not be in any axle column, and the track needs re-judging at
        # the vehicle-type level rather than being forced into a truck class.
        "to_class": {"2_axle": "2Axle_Truck", "3_axle": "3Axle_Truck",
                     "4_or_more_axle": "MAV", "not_a_truck": None},
        "frame": FRAME_WIDEST,
        "margin": 0.10,        # tyres sit at the box edge; a tight crop clips the evidence
        "min_labels": 400,
        # `not_a_truck` is a valid ANSWER but not a class to train. It says the detector
        # was wrong about the vehicle type, which is a different question and one already
        # asked upstream by the existence gate and the class judges. Trained as a fourth
        # class it reached precision 0.053 on 12 examples and swallowed 14 real trucks --
        # a rare label the head cannot learn, actively damaging the three it can. The
        # answers are still collected: they mark detections to send back for re-judging.
        "train_exclude": ["not_a_truck"],
        "hint": "Count wheel GROUPS along the side, not tyres — a rear dual is one group. "
                "One rear group = 2 axles, two close together = 3.",
    },
    "bus_operator": {
        "kind": "attribute",
        # Buses are a handful per hour, so every one can be looked at for less effort than
        # collecting the labels a model would need. Revisit if a station turns out to be
        # bus-heavy -- the labels accumulate either way, and switching to "model" costs
        # nothing but a training run.
        "mode": "human",
        "label": "Bus operator",
        "parents": ["Bus", "Mini_Bus"],
        "values": ["apsrtc", "private", "unclear"],
        "frame": FRAME_LARGEST,
        "margin": 0.04,
        "min_labels": 250,
        "hint": "APSRTC/state buses carry government livery and a route board; private "
                "coaches and school buses do not.",
    },
    "auto_seats": {
        "kind": "attribute",
        # Retired on evidence, not on a hunch: 67 three-wheelers were looked at and the
        # count of 7-seater maxis was ZERO. There is nothing here to model and nothing for
        # a person to check either, so the question is not asked at all -- every 3W_Auto
        # is reported as an auto. Kept in the file rather than deleted because the answer
        # is station-specific: a city section may be full of maxis, and then this becomes
        # `mode: "model"` and the machinery already works.
        #
        # The same 67 crops turned up a different and larger problem. Nine were not
        # passenger autos at all -- goods/load three-wheelers, small buses and tractors
        # that the detector calls 3W_Auto. That is a 13% impurity in the parent class,
        # which is a detector error worth far more than the seat count ever was, and it is
        # tracked as such rather than buried in this attribute's abstentions.
        "mode": "off",
        "label": "Auto size",
        "parents": ["3W_Auto"],
        "values": ["maxi", "normal", "unclear"],
        "frame": FRAME_WIDEST,      # body length is the tell, so profile beats size
        "margin": 0.06,
        "min_labels": 250,
        "hint": "A 7-seater maxi has a visibly longer body and a bigger passenger cabin "
                "than an ordinary 3-seater auto-rickshaw.",
    },
    "goods_size": {
        "kind": "class",
        "mode": "model",
        "label": "Light goods vs car",
        # Measured at KDP-01 on 45 minutes of human-verified footage: of 16 real LCVs the
        # detector found 1-3, and the rest came back as Car_Jeep_Van. It is not a detection
        # failure -- every one of those LCVs was boxed and counted -- so the vehicle is
        # present in the total and sitting in the wrong column. Both v4 and v5 do it, so it
        # is a family-wide weakness and not something a detector swap fixes.
        #
        # 2Axle_Truck is a parent too, in the other direction: three of the LCVs were
        # pushed the other way into the truck classes, where they then reached the axle
        # head and became its only two errors.
        "parents": ["Car_Jeep_Van", "LCV", "2Axle_Truck"],
        # `neither` is the escape hatch, and it is not optional. The parents are what the
        # DETECTOR called the vehicle, so the queue contains its mistakes: a passenger
        # auto-rickshaw called Car_Jeep_Van is plainly visible and plainly none of the
        # three answers. Without this the labeller is forced onto `unclear`, which then
        # conflates "too far to judge" with "wrong vehicle entirely" -- and since unclear
        # is dropped from training, the detector error is silently discarded instead of
        # recorded. Same role `not_a_truck` plays for axles, and excluded from training
        # for the same reason: it is a rare, ragged class that damages the three real ones.
        "values": ["car_jeep_van", "lcv", "truck", "neither", "unclear"],
        "to_class": {"car_jeep_van": "Car_Jeep_Van", "lcv": "LCV",
                     "truck": "2Axle_Truck", "neither": None},
        "train_exclude": ["neither"],
        # The distinguishing evidence is body shape in profile -- a load bed or box body
        # behind the cab, versus a continuous passenger cabin. That reads side-on, exactly
        # like axles, so the widest box in the track wins over the largest.
        "frame": FRAME_WIDEST,
        "margin": 0.08,
        # Don't queue what cannot be answered. Candidate tracks include vehicles at the
        # far end of the road that never came near the line -- at KDP-01 the widths are
        # bimodal, a median of 66px against a p75 of 528 -- and asking about those spends
        # a person's attention on a guess. Set from the labeller's own experience of
        # working down to the bottom of the first batch, not from theory.
        "min_box_w": 400,
        # Three trainable classes, same shape of problem as axles, which needed 400 before
        # the trainer would agree to run. No reason to be braver here.
        "min_labels": 400,
        "hint": "An LCV has a separate load bed or box body behind the cab (Tata Ace, "
                "Bolero pickup, small tempo). A Car_Jeep_Van has one continuous cabin "
                "over its whole length. If it needs more than two axles' worth of chassis "
                "and carries goods, it is a truck. Press 'neither' when it is none of "
                "those — an auto, a bus, a bike — the detector got the vehicle wrong. "
                "'unclear' means you cannot see it well enough to say.",
    },
    "car_use": {
        "kind": "attribute",
        # Not trainable from this footage and unlikely to become so: yellow-board taxis are
        # scarce on these rural sections, and the evidence is a number-plate colour that is
        # a few pixels wide at this camera distance. Both problems point the same way --
        # a person decides, on the small number of cars where it is even legible.
        "mode": "human",
        "label": "Car use",
        "parents": ["Car_Jeep_Van"],
        "values": ["taxi", "private", "unclear"],
        "frame": FRAME_LARGEST,     # the number plate is the evidence; it needs pixels
        "margin": 0.04,
        "min_labels": 250,
        "hint": "Indian commercial taxis carry yellow number plates or a yellow board; "
                "private cars have white plates.",
    },
}

# Every attribute keeps "unclear" as an answer, and it is never a training label. A
# labeller forced to choose will choose, and a guess recorded as ground truth is worse
# than a gap -- it is a gap that looks like data.
ABSTAIN = "unclear"


def spec(name):
    if name not in ATTRIBUTES:
        raise KeyError(f"unknown attribute {name!r}; have {sorted(ATTRIBUTES)}")
    return ATTRIBUTES[name]


def trainable_values(name):
    """The classes a model actually learns.

    Neither the abstention nor any `train_exclude` value is one of them: both record
    something real about the crop, and neither is an answer the model should ever emit.
    """
    s = spec(name)
    skip = {ABSTAIN, *s.get("train_exclude", [])}
    return [v for v in s["values"] if v not in skip]


def for_class(class_name):
    """Which questions apply to a vehicle of this MoRTH class."""
    return [n for n, s in ATTRIBUTES.items() if class_name in s["parents"]]
