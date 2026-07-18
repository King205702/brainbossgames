/* =========================================================
   BRAIN BOSS — script.js
   ========================================================= */

// -----------------------------------------------------------
// 🔧 CONFIG — point this at your Render backend before deploy
// -----------------------------------------------------------
const BACKEND_URL = "https://YOUR-RENDER-BACKEND-URL.onrender.com";

// Expected backend contract:
//   POST  {BACKEND_URL}/api/verify-code    body: { code }
//         -> 200 { valid: true, play_token: "..." }
//         -> 200/4xx { valid: false, message: "..." }
//
//   POST  {BACKEND_URL}/api/submit-answer  body: { code, play_token, formula }
//         -> 200 { success: true, correct: bool, won: bool, discount_code: string|null, message: "..." }
//         -> 4xx { success: false, message: "..." }
// -----------------------------------------------------------

let currentAccessCode = null; // set once a code is verified
let currentPlayToken = null;  // set once a code is verified, required to submit

/* =========================================================
   1. ACCESS CODE GATE
   ========================================================= */

const gateOverlay = document.getElementById("access-gate");
const codeForm = document.getElementById("code-form");
const codeInputs = Array.from(document.querySelectorAll(".code-input"));
const verifyBtn = document.getElementById("verify-btn");
const gateError = document.getElementById("gate-error");
const gameApp = document.getElementById("game");

function initCodeInputs() {
  codeInputs.forEach((input, i) => {
    input.addEventListener("input", () => {
      // keep digits only
      input.value = input.value.replace(/[^0-9]/g, "").slice(0, 1);
      input.classList.toggle("filled", input.value !== "");
      if (input.value && i < codeInputs.length - 1) {
        codeInputs[i + 1].focus();
      }
      clearGateError();
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && input.value === "" && i > 0) {
        codeInputs[i - 1].focus();
      }
      if (e.key === "Enter") {
        e.preventDefault();
        codeForm.requestSubmit();
      }
    });

    // allow pasting the full code into any box
    input.addEventListener("paste", (e) => {
      const pasted = (e.clipboardData || window.clipboardData).getData("text");
      const digits = pasted.replace(/[^0-9]/g, "").slice(0, 6).split("");
      if (digits.length > 1) {
        e.preventDefault();
        digits.forEach((d, idx) => {
          if (codeInputs[idx]) {
            codeInputs[idx].value = d;
            codeInputs[idx].classList.add("filled");
          }
        });
        const nextEmpty = codeInputs.find((el) => el.value === "");
        (nextEmpty || codeInputs[codeInputs.length - 1]).focus();
      }
    });
  });
}

function getEnteredCode() {
  return codeInputs.map((el) => el.value).join("");
}

function showGateError(message) {
  gateError.textContent = message;
  codeInputs.forEach((el) => {
    el.classList.add("shake");
    setTimeout(() => el.classList.remove("shake"), 350);
  });
}

function clearGateError() {
  gateError.textContent = "";
}

function setVerifying(isVerifying) {
  verifyBtn.disabled = isVerifying;
  verifyBtn.classList.toggle("loading", isVerifying);
}

async function verifyCode(code) {
  setVerifying(true);
  clearGateError();

  try {
    const res = await fetch(`${BACKEND_URL}/api/verify-code`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });

    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      /* non-JSON response, fall through to generic error */
    }

    if (res.ok && data.valid) {
      currentAccessCode = code;
      currentPlayToken = data.play_token;
      unlockGame();
    } else {
      showGateError(data.message || "Invalid or expired code. Double-check and try again.");
    }
  } catch (err) {
    console.error("Verify request failed:", err);
    showGateError("Couldn't reach the server. Check your connection and try again.");
  } finally {
    setVerifying(false);
  }
}

function unlockGame() {
  gateOverlay.setAttribute("hidden", "");
  gameApp.removeAttribute("aria-hidden");
  gameApp.hidden = false;
  initGame();
}

codeForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const code = getEnteredCode();
  if (code.length !== 6) {
    showGateError("Enter all 6 digits.");
    return;
  }
  verifyCode(code);
});

initCodeInputs();
codeInputs[0].focus();

/* =========================================================
   2. MATCHSTICK GAME
   Digits are modelled as a 7-segment layout:
     top, ul, ur, mid, ll, lr, bottom
   Starting equation:  6 + 4 = 4   (14 sticks total)
   Winning equation:   5 + 4 = 9   (14 sticks total — the SAME
   stick removed from the lower-left of the "6" becomes the
   top stick of the "9", drawn open-bottom, which is the
   classic matchstick-font 9.)
   ========================================================= */

const SEGMENTS = ["top", "ul", "ur", "mid", "ll", "lr", "bottom"];

let digitState = null; // set in initGame()
let selectedStick = null; // { digit, seg } | null
let locked = false;

function startingState() {
  return {
    A: { top: 1, ul: 1, ur: 0, mid: 1, ll: 1, lr: 1, bottom: 1 }, // 6
    B: { top: 0, ul: 1, ur: 1, mid: 1, ll: 0, lr: 1, bottom: 0 }, // 4
    C: { top: 0, ul: 1, ur: 1, mid: 1, ll: 0, lr: 1, bottom: 0 }, // 4
  };
}

const TARGET_STATE = {
  A: { top: 1, ul: 1, ur: 0, mid: 1, ll: 0, lr: 1, bottom: 1 }, // 5
  B: { top: 0, ul: 1, ur: 1, mid: 1, ll: 0, lr: 1, bottom: 0 }, // 4 (unchanged)
  C: { top: 1, ul: 1, ur: 1, mid: 1, ll: 0, lr: 1, bottom: 0 }, // 9 (open bottom)
};

const submitBtn = document.getElementById("submit-btn");
const resultMsg = document.getElementById("result-msg");
const pickupHint = document.getElementById("pickup-hint");
const attemptPill = document.getElementById("attempt-pill");

function initGame() {
  digitState = startingState();
  selectedStick = null;
  locked = false;

  gameApp.classList.remove("locked");
  resultMsg.textContent = "";
  resultMsg.className = "result-msg";
  pickupHint.textContent = "\u00A0";
  submitBtn.disabled = false;

  render();
  attachSegmentListeners();
  submitBtn.addEventListener("click", handleSubmit, { once: true });
}

function attachSegmentListeners() {
  document.querySelectorAll(".segment").forEach((seg) => {
    seg.addEventListener("click", onSegmentClick);
  });
}

function onSegmentClick(e) {
  if (locked) return;

  const el = e.currentTarget;
  const digit = el.dataset.digit;
  const seg = el.dataset.seg;
  const isOccupied = digitState[digit][seg] === 1;

  if (isOccupied) {
    // picking up (or re-selecting / deselecting) a stick
    if (selectedStick && selectedStick.digit === digit && selectedStick.seg === seg) {
      selectedStick = null; // deselect
      pickupHint.textContent = "\u00A0";
    } else {
      selectedStick = { digit, seg };
      pickupHint.textContent = "Stick picked up — tap an empty slot to place it.";
    }
    render();
    return;
  }

  // empty slot clicked
  if (!selectedStick) {
    pickupHint.textContent = "Pick up a lit matchstick first.";
    return;
  }

  // perform the move: clear old position, fill new position
  digitState[selectedStick.digit][selectedStick.seg] = 0;
  digitState[digit][seg] = 1;

  const placedKey = `${digit}-${seg}`;
  selectedStick = null;
  pickupHint.textContent = "\u00A0";
  render(placedKey);
}

function render(justPlacedKey) {
  document.querySelectorAll(".segment").forEach((el) => {
    const digit = el.dataset.digit;
    const seg = el.dataset.seg;
    const occupied = digitState[digit][seg] === 1;
    const isSelected =
      selectedStick && selectedStick.digit === digit && selectedStick.seg === seg;

    el.classList.toggle("occupied", occupied);
    el.classList.toggle("selected", !!isSelected);

    if (justPlacedKey === `${digit}-${seg}`) {
      el.classList.add("just-placed");
      setTimeout(() => el.classList.remove("just-placed"), 500);
    }
  });
}

function statesEqual(a, b) {
  return SEGMENTS.every(
    (seg) =>
      a.A[seg] === b.A[seg] && a.B[seg] === b.B[seg] && a.C[seg] === b.C[seg]
  );
}

// Maps a sorted, comma-joined list of lit segments to the digit they form.
// Used to turn the current board into a plain "5+4=9" style string that the
// server checks — the server is the source of truth for win/lose, never the
// browser.
const DIGIT_SHAPES = {
  "bottom,ll,lr,top,ul,ur": "0",
  "lr,ur": "1",
  "bottom,ll,mid,top,ur": "2",
  "bottom,lr,mid,top,ur": "3",
  "lr,mid,ul,ur": "4",
  "bottom,lr,mid,top,ul": "5",
  "bottom,ll,lr,mid,top,ul": "6",
  "lr,top,ur": "7",
  "bottom,ll,lr,mid,top,ul,ur": "8",
  "bottom,lr,mid,top,ul,ur": "9", // standard closed-bottom 9
  "lr,mid,top,ul,ur": "9",        // our open-bottom 9 (the puzzle's target shape)
};

function decodeDigit(digit) {
  const litSegments = SEGMENTS.filter((seg) => digit[seg] === 1).sort();
  return DIGIT_SHAPES[litSegments.join(",")] || "?";
}

function getEquationString() {
  return `${decodeDigit(digitState.A)}+${decodeDigit(digitState.B)}=${decodeDigit(digitState.C)}`;
}

/* =========================================================
   3. SUBMIT — one try only. The server is the sole judge of
   correctness; this just locks the UI and reports what the
   server says.
   ========================================================= */

async function handleSubmit() {
  if (locked) return;
  locked = true;

  // lock the board immediately, before the network call even starts
  gameApp.classList.add("locked");
  submitBtn.disabled = true;
  attemptPill.textContent = "attempt used";
  resultMsg.textContent = "Submitting your answer...";
  resultMsg.className = "result-msg pending";

  const formula = getEquationString();

  try {
    const res = await fetch(`${BACKEND_URL}/api/submit-answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code: currentAccessCode,
        play_token: currentPlayToken,
        formula,
      }),
    });

    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      /* non-JSON response, fall through to generic error */
    }

    if (!res.ok || !data.success) {
      resultMsg.textContent = data.message || "Something went wrong submitting your answer.";
      resultMsg.className = "result-msg failure";
      return;
    }

    if (data.won) {
      resultMsg.textContent = data.message || "🏆 Correct — you're the winner of this round!";
      resultMsg.className = "result-msg success";
    } else if (data.correct) {
      resultMsg.textContent = data.message || "Correct, but someone else got there first.";
      resultMsg.className = "result-msg almost";
    } else {
      resultMsg.textContent = data.message || "Not quite right. That was your one try.";
      resultMsg.className = "result-msg failure";
    }
  } catch (err) {
    console.error("Submit request failed:", err);
    resultMsg.textContent =
      "Couldn't reach the server to submit your answer. If this persists, contact support with your code.";
    resultMsg.className = "result-msg failure";
  }
}
