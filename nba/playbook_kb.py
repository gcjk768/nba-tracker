"""Basketball playbook knowledge: what common sets / actions / coverages are, and what usually beats each defense.

Source tag for everything here: "coaching principle" (standard basketball teaching), never "measured". The measured
side (what actually scores against teams with each lean this season) comes from schemes.counter_evidence().
"""

PLAYS = {
    # offensive sets and actions
    "horns": ("set", "Two bigs at the elbows, two shooters in the corners, ball handler up top. Opens a pick-and-roll to either side, an elbow handoff or a big-to-big pass."),
    "spain pick-and-roll": ("set", "A pick-and-roll where a third player back-screens the screener's defender, so the roll man gets free and the big who should protect the rim is pulled away."),
    "pick-and-roll": ("action", "A screener sets a ball screen, then rolls to the rim (or pops). The defense must choose how to guard the ball and the screener."),
    "empty-side pick-and-roll": ("action", "A pick-and-roll on the side with no other offensive player, so no help defender is close."),
    "inverted pick-and-roll": ("action", "A pick-and-roll where the big handles the ball and a guard sets the screen, flipping the usual matchups."),
    "pick-and-pop": ("action", "The screener steps out for a jumper instead of rolling to the rim, punishing a big who stays back."),
    "floppy": ("set", "A shooter starts under the basket and runs off staggered or single screens on either side to catch a jumper."),
    "chicago": ("set", "A pin-down screen flowing straight into a dribble handoff, so the shooter catches the ball already moving downhill."),
    "zipper": ("set", "A player cuts from the block straight up to the top of the key off a screen to receive the ball."),
    "double zipper": ("set", "Two zipper cuts at once; often an inbound play to free a shooter at the top."),
    "iverson": ("set", "A wing cuts across the top of the floor off two screens at the elbows to catch on the other side."),
    "elbow": ("set", "Entry pass to a big at the elbow, who becomes the hub for handoffs, cuts and split action."),
    "delay": ("set", "Five-out spacing with the big as a passer at the top; guards cut and run handoffs around him."),
    "pistol": ("set", "Early offense: a guard and a wing on the same side run a quick handoff or pick-and-roll before the defense sets."),
    "ram": ("action", "A player sets an off-ball screen for the eventual screener, who then sets the ball screen: a screen for the screener."),
    "flare": ("set", "A screen that lets a shooter drift away from the ball to the wing for a catch-and-shoot three."),
    "empty flare": ("action", "A flare screen on the empty side of the floor."),
    "stagger": ("set", "Two screens set one after another for the same cutter, usually to free a shooter."),
    "hammer": ("set", "A baseline drive while a shooter gets a back screen into the far corner for a kick-out three."),
    "ghost screen": ("action", "The screener fakes the ball screen and sprints out, punishing a defense that switches or hedges early."),
    "5-out": ("set", "All five players outside the arc, emptying the paint for drives and cuts."),
    "dho": ("action", "Dribble handoff: the ball handler hands off to a teammate running past, with the body as the screen."),
    "dribble handoff": ("action", "The ball handler hands off to a teammate running past, with the body as the screen."),
    "rip dho": ("action", "A cut or rip screen right before the dribble handoff to gain an extra step."),
    "zoom": ("action", "A pin-down into a dribble handoff (same idea as Chicago)."),
    "zoom action": ("action", "A pin-down into a dribble handoff (same idea as Chicago)."),
    "split action": ("action", "After a post entry, the passer screens for a teammate; they split to cut and pop around the post."),
    "split cut": ("action", "Two players cross after a post entry, one cutting and one popping."),
    "ucla cut": ("action", "The passer cuts off a high-post back screen straight to the rim."),
    "veer": ("action", "A screener fakes a ball screen and runs into an off-ball screen instead."),
    "peja": ("action", "A back screen for a shooter followed by a handoff, made famous by Peja Stojakovic."),
    "wide": ("action", "An off-ball screen that brings a player up toward the ball."),
    "back screen": ("action", "A screen set behind a defender so the cutter can slip to the rim."),
    "backdoor cuts": ("action", "A cut behind an overplaying defender toward the basket."),
    "pin-down": ("action", "A downward screen near the block to free a shooter coming up to the wing."),
    "isolation": ("action", "Clear one side for a scorer to go one-on-one."),
    "post-up": ("action", "Feed a player with his back to the basket near the block."),
    "lob": ("action", "A high pass toward the rim for a finisher, usually after a roll or a cut."),
    "blind pig": ("action", "A pass to a big at the high post while a cutter goes backdoor behind his defender."),
    "nova": ("set", "Knicks name for a weave-based motion entry built on dribble handoffs."),
    "motion offense": ("set", "Read-and-react offense: players pass, cut and screen by rules rather than a fixed set."),
    "quick action": ("action", "A short set designed to get a shot quickly, often after a timeout."),
    "pull-up": ("action", "The ball handler stops off the dribble and shoots a jumper, often coming off a ball screen."),
    "spot-up jumper": ("action", "A shooter waits on the perimeter and shoots on the catch after a kick-out or swing pass."),
    "drive": ("action", "Attack the basket off the dribble to finish or to draw help and kick out."),
    "cut": ("action", "A player without the ball darts to the rim when his defender looks away or overplays."),
    "putback": ("action", "Grab an offensive rebound and score right away."),
    "rim finish": ("action", "A layup or dunk at the rim that is not off a drive, cut or lob (often a dump-off pass)."),
    "alley-oop": ("action", "A pass thrown above the rim for a teammate to catch and finish in the air."),
    "post-up / turnaround": ("action", "Back-to-the-basket scoring: post moves, turnarounds, fadeaways and hooks."),
    "run shooters off": ("coverage", "Defenders close out hard and high on shooters to take away the three, accepting drives."),
    "pack the paint": ("coverage", "Help defenders sit in the lane to wall off drives, accepting kick-out threes."),
    "turnover-forcing": ("coverage", "Aggressive ball pressure, traps and passing-lane gambles to create turnovers."),
    "rim protector": ("coverage", "One shot-blocking big stays near the rim and erases mistakes."),
    # defensive coverages
    "drop": ("coverage", "The big drops back toward the paint on pick-and-rolls to protect the rim, conceding mid-range pull-ups."),
    "switch": ("coverage", "Defenders swap assignments on screens instead of fighting through, which can leave mismatches."),
    "blitz": ("coverage", "Two defenders trap the ball handler on the screen to force the ball out of his hands."),
    "hedge": ("coverage", "The big jumps out hard to slow the ball handler, then recovers to his man."),
    "show": ("coverage", "The big shows briefly to slow the ball handler, then recovers to his man."),
    "ice": ("coverage", "Side pick-and-rolls are forced toward the sideline and baseline, away from the middle."),
    "at the level": ("coverage", "The big meets the ball handler at the screen instead of dropping, then recovers."),
    "zone": ("coverage", "Defenders guard areas rather than players."),
    "2-3 zone": ("coverage", "Two defenders up top, three along the baseline, guarding areas."),
    "box-and-one": ("coverage", "Four players in a zone box, one face-guarding the opponent's best scorer."),
    "full-court press": ("coverage", "Pressure the ball the length of the floor to force turnovers or burn clock."),
}

# what usually beats each defense type: (counters, why). Keys cover reported coverages and our inferred leans.
COUNTERS = {
    "drop": (["pick-and-pop", "pull-up", "spain pick-and-roll", "empty-side pick-and-roll"],
             "The big sits back, so the ball handler gets an open pull-up and a popping screener is left alone."),
    "switch": (["isolation", "post-up", "ghost screen", "back screen"],
               "Switching creates mismatches to attack one-on-one, and fake or slipped screens catch defenders switching early."),
    "blitz": (["pick-and-roll", "lob", "5-out"],
              "Two defenders are on the ball, so a quick pass to the rolling screener leaves a 4-on-3 behind the trap."),
    "hedge": (["pick-and-pop", "ghost screen", "back screen"],
              "The big is stepping up high, so slipping or popping the screener leaves him behind the play."),
    "ice": (["pick-and-pop", "flare", "inverted pick-and-roll"],
            "Ice pushes the ball sideways, so the screener popping or re-screening flips the angle back to the middle."),
    "zone": (["elbow", "flare", "5-out", "split action"],
             "Flash a player to the high post or short corner, then skip the ball to the weak side for open threes."),
    "pack the paint": (["spot-up jumper", "flare", "5-out", "pick-and-pop"],
                       "Help sits in the lane, so kick-outs to shooters and popping bigs get open threes."),
    "run shooters off": (["drive", "backdoor cuts", "pick-and-roll"],
                         "Closeouts run hard at shooters, so pump-fake drives and backdoor cuts beat the rotation."),
    "turnover-forcing": (["dho", "quick action", "pick-and-roll"],
                         "Against traps and gambles, quick handoffs and early passes move the ball before pressure arrives."),
    "rim protector": (["pick-and-pop", "flare", "isolation"],
                      "Pull the shot-blocker away from the rim with a popping screener, or take him out of the action entirely."),
}

LEAN_TO_COVERAGE = {   # our inferred defense labels -> the counter row that applies
    "Concedes the mid-range (drop-style)": "drop",
    "Turnover-forcing (aggressive, traps or blitzes likely)": "turnover-forcing",
    "Packs the paint": "pack the paint",
    "Runs shooters off the line": "run shooters off",
}


def lean_key(label: str) -> str | None:
    if label.startswith("Rim protector anchored"):
        return "rim protector"
    return LEAN_TO_COVERAGE.get(label)


def describe(label: str) -> dict:
    kind, desc = PLAYS.get(label, (None, None))
    return {"label": label, "kind": kind, "desc": desc}
