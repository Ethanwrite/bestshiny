/**
 * Public website. Independent of the application shell: it owns its own
 * header, its own routes and its own markup, and it never touches workspace
 * state. The only thing it shares with the app is the token layer in
 * styles.css.
 */
import { onRoute, navigate, currentUser, AUTH_ROUTES } from "./router.js";

const mount = () => document.getElementById("publicPages");

/* The workspace mock on the landing page is a real screenshot of the shell's
   structure, drawn in DOM so it stays honest when the shell changes. */
const workspaceShot = () => `
  <div class="pub-shot" id="pubShot">
    <div class="pub-shot-bar">
      <div class="pub-shot-dots"><i></i><i></i><i></i></div>
      <div class="pub-shot-tabs"><span>Create</span><span class="on" data-shot-tab="director">Director</span><span data-shot-tab="productions">Productions</span></div>
      <span class="pub-shot-credits">2,480 credits</span>
    </div>
    <div class="pub-shot-body">
      <div class="pub-shot-rail">
        <span class="pub-shot-label">SCENES &amp; SHOTS</span>
        <div class="pub-shot-row"><i class="ok"></i>SC 01 · Awning</div>
        <div class="pub-shot-row on" data-shot-node="1"><i class="run"></i>SH 01 · Desire line</div>
        <div class="pub-shot-row" data-shot-node="2"><i></i>SH 02 · Wall-hug track</div>
        <div class="pub-shot-row" data-shot-node="3"><i></i>SH 03 · Blade flash</div>
        <div class="pub-shot-row"><i class="ok"></i>SC 02 · Clinic</div>
        <div class="pub-shot-row"><i></i>SH 04 · Intent poured back</div>
      </div>
      <div class="pub-shot-stage">
        <div class="pub-shot-frame"></div>
        <span class="pub-shot-caption" id="pubShotCaption">SHOT 01 · 9:16 · GENERATING</span>
      </div>
      <div class="pub-shot-rail right">
        <span class="pub-shot-label">INSPECTOR</span>
        <div class="pub-shot-row"><i class="ok"></i>Character locked</div>
        <div class="pub-shot-row"><i class="ok"></i>Scene reference</div>
        <div class="pub-shot-row"><i class="run"></i>Continuity: hard</div>
        <div class="pub-shot-row"><i></i>Camera: slow push</div>
        <div class="pub-shot-row"><i></i>Lighting: cold side</div>
      </div>
    </div>
    <div class="pub-shot-bottom">
      <div class="pub-shot-fact"><span>Duration</span><b id="pubShotDuration">4.0s</b></div>
      <div class="pub-shot-fact"><span>Model</span><b id="pubShotModel">Auto · Veo</b></div>
      <div class="pub-shot-fact"><span>Resolution</span><b>1080p</b></div>
      <div class="pub-shot-fact"><span>Estimated</span><b>18 credits</b></div>
      <span class="pub-shot-generate" id="pubShotCta">Generate shot</span>
    </div>
  </div>`;

const WORKFLOW = [
  ["01", "Script", "Paste a scene. It is parsed, never paraphrased."],
  ["02", "Scene", "Locations, time of day and who is present."],
  ["03", "Shot", "An ordered, producible list with start and end state."],
  ["04", "Generate", "The right model for this shot, with your references bound."],
  ["05", "Review", "Character, camera and action scored side by side."],
  ["06", "Approve", "One variant commits to the timeline and becomes canon."],
];

const ROUTES = [
  ["Dialogue, tight coverage", "Face must not drift", "Veo"],
  ["Fast action, 30s single take", "No cut inside the beat", "Wan"],
  ["Product hero, controlled specular", "Material fidelity", "Seedance"],
  ["Key visual, 9:16 poster frame", "Still, not motion", "GPT Image"],
  ["Stylised opener, loose realism", "Look over likeness", "Grok"],
  ["Continuity bridge between shots", "Same light, same wardrobe", "Kling"],
];

const MODELS = ["Seedance", "Veo", "Wan", "Kling", "GPT Image", "Grok", "Runway", "Flux"];

function landing() {
  return `
  <section class="pub-hero">
    <div class="pub-hero-inner">
      <span class="pub-eyebrow">AI Video Production Workspace</span>
      <h1>AI Video Production, from <em>Script to Final Shot.</em></h1>
      <p class="pub-lede">
        Best Shiny turns a script, your reference assets, your characters and your creative
        direction into AI video shots you can actually cut with. Not a prompt box — a workspace
        that remembers what a scene looked like three shots ago.
      </p>
      <div class="pub-hero-cta">
        <a class="btn btn-primary" href="/signup" data-link>Start Creating</a>
        <button class="btn btn-secondary" type="button" id="watchDemoBtn">Watch Demo</button>
      </div>
      <p class="pub-hero-note">New workspaces start with free credits. No card, no subscription.</p>
      ${workspaceShot()}
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">The workspace</span>
      <h2 class="pub-h2">Three surfaces, one production.</h2>
      <p class="pub-lede">Every screen works on the same objects — scenes, shots, characters, assets — so nothing has to be described twice.</p>
      <div class="pub-cap-grid">
        <article class="pub-cap pub-cap-accent-1">
          <span class="pub-cap-no">01</span>
          <h3>Create</h3>
          <p>Write a frame, drop a reference, pick a ratio. The canvas is the result, not a form.</p>
          <ul>
            <li>Prompt improvement that adds lens, light and material — and leaves your subject alone</li>
            <li>Reference images bound to the request, not pasted into it</li>
            <li>Anything good becomes a project asset with a traceable version</li>
          </ul>
        </article>
        <article class="pub-cap pub-cap-accent-2">
          <span class="pub-cap-no">02</span>
          <h3>BestShiny Director</h3>
          <p>A script becomes an ordered shot list that knows what state each shot opens and closes in.</p>
          <ul>
            <li>Scene and shot tree, with the original script one click away</li>
            <li>Continuity policy chosen per cut, not applied globally</li>
            <li>Variants scored on character, camera and action before anything is approved</li>
          </ul>
        </article>
        <article class="pub-cap pub-cap-accent-3">
          <span class="pub-cap-no">03</span>
          <h3>Production</h3>
          <p>Every generation is a job with a price, a state and a way back if it fails.</p>
          <ul>
            <li>Running, queued, completed and failed in one list</li>
            <li>Safe retry that reuses the same submission instead of paying twice</li>
            <li>Credits reserved on submit, settled on result</li>
          </ul>
        </article>
      </div>
    </div>
  </section>

  <section class="pub-section" id="workflow">
    <div class="pub-inner">
      <span class="pub-eyebrow">How a shot gets made</span>
      <h2 class="pub-h2">Script → Scene → Shot → Generate → Review → Approve.</h2>
      <p class="pub-lede">The same six steps every time, whether it is one frame or a forty-shot episode. Nothing advances until you approve it.</p>
      <div class="pub-flow">
        ${WORKFLOW.map(([no, name, copy]) => `
          <div class="pub-flow-step" data-flow="${no}">
            <b>${no}</b><strong>${name}</strong><p>${copy}</p>
          </div>`).join("")}
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner pub-router">
      <div>
        <span class="pub-eyebrow">Model routing</span>
        <h2 class="pub-h2">One workflow. Multiple AI models.</h2>
        <p class="pub-lede">
          You describe the shot. The platform reads what that shot actually needs — length,
          motion, whether a face has to hold — and routes it to the model that can deliver it.
          You can always override. You are never silently switched.
        </p>
        <p class="pub-lede">Models are an implementation detail of a shot, not the product.</p>
      </div>
      <div class="pub-router-board">
        ${ROUTES.map(([need, why, model]) => `
          <div class="pub-route">
            <div class="pub-route-need"><strong>${need}</strong><span>${why}</span></div>
            <span class="pub-route-arrow" aria-hidden="true">→</span>
            <div class="pub-route-model"><b>${model}</b><i></i></div>
          </div>`).join("")}
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner pub-split">
      <div>
        <span class="pub-eyebrow">Continuity</span>
        <h2 class="pub-h2">Character and scene continuity that survives the cut.</h2>
        <p class="pub-lede">
          A character is an object with a canonical reference, not a sentence you retype. Lock a
          face, a location or a wardrobe state once and every later shot inherits it — and gets
          checked against it before the result is allowed into your timeline.
        </p>
        <p class="pub-lede">When a cut is risky, the director says so and proposes the safer join.</p>
      </div>
      <div class="pub-continuity">
        <div class="pub-continuity-row locked">
          <span class="pub-continuity-chip"></span>
          <div class="pub-continuity-body"><strong>Cheng Mu — identity v3</strong><span>Face mesh · wardrobe · amber eye detail</span></div>
          <span class="pub-continuity-state">CANONICAL</span>
        </div>
        <div class="pub-continuity-row locked">
          <span class="pub-continuity-chip"></span>
          <div class="pub-continuity-body"><strong>Neon rain street</strong><span>Wide · medium · detail, one light direction</span></div>
          <span class="pub-continuity-state">CANONICAL</span>
        </div>
        <div class="pub-continuity-row">
          <span class="pub-continuity-chip"></span>
          <div class="pub-continuity-body"><strong>Shot 04 → Shot 05</strong><span>Camera crosses the axis · re-anchor proposed</span></div>
          <span class="pub-continuity-state" style="color:var(--brand)">CHECKED</span>
        </div>
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner" style="text-align:center">
      <span class="pub-eyebrow" style="justify-content:center">Routed across</span>
      <div class="pub-logos">
        ${MODELS.map((name) => `<span class="pub-logo"><i></i>${name}</span>`).join("")}
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">Pricing</span>
      <h2 class="pub-h2">Credits, not seats.</h2>
      <p class="pub-lede">You pay for the generations you keep running. Credits never expire and there is no auto-renewal.</p>
      <div class="pub-price-grid">
        <article class="pub-price">
          <span class="pub-price-tag">FREE</span>
          <div class="pub-price-figure"><strong>$0</strong><span>to start</span></div>
          <ul>
            <li>Starter credits on signup</li>
            <li>Full Create and Director workspace</li>
            <li>One project, image and short video</li>
          </ul>
          <a class="btn btn-secondary btn-full" href="/signup" data-link>Create a workspace</a>
        </article>
        <article class="pub-price featured">
          <span class="pub-price-tag">PRO</span>
          <div class="pub-price-figure"><strong>$30</strong><span>one payment</span></div>
          <ul>
            <li>Unlocks Pro and adds 3,000 credits</li>
            <li>Every model the router can reach</li>
            <li>Priority queue and full production history</li>
          </ul>
          <a class="btn btn-primary btn-full" href="/pricing" data-link>See what a shot costs</a>
        </article>
      </div>
      <p class="pub-price-note">1 credit = $0.01. Credits are reserved when a job is submitted and settled against the real provider cost.</p>
    </div>
  </section>

  <section class="pub-cta">
    <h2>Your next shot starts here.</h2>
    <p>Bring a script, or bring one line. The workspace handles the twelve steps between that and a shot you can cut.</p>
    <div class="btn-row">
      <a class="btn btn-primary" href="/signup" data-link>Start Creating</a>
      <a class="btn btn-secondary" href="/product" data-link>See the workspace</a>
    </div>
  </section>`;
}

function productPage() {
  return `
  <section class="pub-section" style="padding-top:72px">
    <div class="pub-inner">
      <span class="pub-eyebrow">Product</span>
      <h2 class="pub-h2">A workspace built around shots, not prompts.</h2>
      <p class="pub-lede">
        Most AI video tools give you a text box and a queue. Best Shiny gives you the objects a
        production actually runs on — scenes, shots, characters, references, variants — and keeps
        them consistent from the first line of script to the approved take.
      </p>
      <div class="pub-cap-grid">
        <article class="pub-cap pub-cap-accent-1">
          <span class="pub-cap-no">SIDEBAR</span>
          <h3>Objects</h3>
          <p>Scenes and shots as a tree, characters and references as assets. Click one and everything else follows it.</p>
        </article>
        <article class="pub-cap pub-cap-accent-2">
          <span class="pub-cap-no">CANVAS</span>
          <h3>The work</h3>
          <p>The current shot fills the page. Preview, director notes, state and variants — nothing competing for the same attention.</p>
        </article>
        <article class="pub-cap pub-cap-accent-3">
          <span class="pub-cap-no">INSPECTOR</span>
          <h3>Properties</h3>
          <p>Whatever is selected, its properties are on the right: character, camera, lighting, composition, continuity, model.</p>
        </article>
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">Production flow</span>
      <h2 class="pub-h2">Six steps, and you approve every one.</h2>
      <div class="pub-flow">
        ${WORKFLOW.map(([no, name, copy]) => `
          <div class="pub-flow-step"><b>${no}</b><strong>${name}</strong><p>${copy}</p></div>`).join("")}
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">What is actually enforced</span>
      <h2 class="pub-h2">Guarantees, not vibes.</h2>
      <div class="pub-table-wrap">
        <table class="pub-table">
          <thead><tr><th>Guarantee</th><th>What it means in the product</th></tr></thead>
          <tbody>
            <tr><td><strong>Your model choice stands</strong></td><td><span>A model you pick is never silently replaced by a cheaper one. If it cannot run, you are told why.</span></td></tr>
            <tr><td><strong>Originals are never re-encoded</strong></td><td><span>Provider size limits are met with derived renditions. Your master file stays byte-identical.</span></td></tr>
            <tr><td><strong>One submission, one charge</strong></td><td><span>A dropped connection never buys the same shot twice — retries reuse the original submission.</span></td></tr>
            <tr><td><strong>Canonical is explicit</strong></td><td><span>A new version never becomes the project reference unless you tick the box that says so.</span></td></tr>
            <tr><td><strong>Approval is human</strong></td><td><span>When automated checks cannot decide, the shot waits for you and records the reason you gave.</span></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="pub-cta">
    <h2>Your next shot starts here.</h2>
    <p>Open a workspace and compile your first scene in a couple of minutes.</p>
    <div class="btn-row"><a class="btn btn-primary" href="/signup" data-link>Start Creating</a></div>
  </section>`;
}

function modelsPage() {
  return `
  <section class="pub-section" style="padding-top:72px">
    <div class="pub-inner">
      <span class="pub-eyebrow">Models</span>
      <h2 class="pub-h2">One workflow. Multiple AI models.</h2>
      <p class="pub-lede">
        The router reads a shot's requirements — duration, motion, whether identity has to hold,
        whether it continues the previous frame — and ranks the models that can serve it. What is
        available depends on what your workspace has configured.
      </p>
      <div class="pub-router-board" style="margin-top:36px">
        ${ROUTES.map(([need, why, model]) => `
          <div class="pub-route">
            <div class="pub-route-need"><strong>${need}</strong><span>${why}</span></div>
            <span class="pub-route-arrow" aria-hidden="true">→</span>
            <div class="pub-route-model"><b>${model}</b><i></i></div>
          </div>`).join("")}
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">Routing inputs</span>
      <h2 class="pub-h2">What the router actually reads.</h2>
      <div class="pub-table-wrap">
        <table class="pub-table">
          <thead><tr><th>Signal</th><th>Effect on the route</th></tr></thead>
          <tbody>
            <tr><td><strong>Shot duration</strong></td><td><span>A beat longer than a model's native single-shot envelope is routed away from it rather than cut in half.</span></td></tr>
            <tr><td><strong>Identity requirement</strong></td><td><span>A locked character pushes the shot toward models that accept a reference image and hold a face.</span></td></tr>
            <tr><td><strong>Continuity policy</strong></td><td><span>A hard join to the previous shot prefers image-to-video from the committed end frame.</span></td></tr>
            <tr><td><strong>Criticality</strong></td><td><span>Hero and canonical work raises the trust bar a provider must clear before its result is accepted.</span></td></tr>
            <tr><td><strong>Configuration</strong></td><td><span>A model with no credentials is excluded from ranking. Not configured is a grey state, not a failure.</span></td></tr>
          </tbody>
        </table>
      </div>
      <div class="pub-logos" style="justify-content:flex-start">
        ${MODELS.map((name) => `<span class="pub-logo"><i></i>${name}</span>`).join("")}
      </div>
      <p class="pub-price-note">Model names are the routes the platform knows how to speak to. Availability in your workspace depends on the credentials it holds.</p>
    </div>
  </section>

  <section class="pub-cta">
    <h2>Your next shot starts here.</h2>
    <p>Pick the model yourself, or let the shot decide.</p>
    <div class="btn-row"><a class="btn btn-primary" href="/signup" data-link>Start Creating</a></div>
  </section>`;
}

function pricingPage() {
  return `
  <section class="pub-section" style="padding-top:72px">
    <div class="pub-inner">
      <span class="pub-eyebrow">Pricing</span>
      <h2 class="pub-h2">Credits, not seats.</h2>
      <p class="pub-lede">1 credit = $0.01. Credits are reserved when a job is submitted and settled against what the provider actually charged. Nothing recurs.</p>
      <div class="pub-price-grid">
        <article class="pub-price">
          <span class="pub-price-tag">FREE</span>
          <div class="pub-price-figure"><strong>$0</strong><span>to start</span></div>
          <ul>
            <li>Starter credits on signup</li>
            <li>Create and Director workspace in full</li>
            <li>Image generation and short video</li>
            <li>Production history and safe retry</li>
          </ul>
          <a class="btn btn-secondary btn-full" href="/signup" data-link>Create a workspace</a>
        </article>
        <article class="pub-price featured">
          <span class="pub-price-tag">PRO</span>
          <div class="pub-price-figure"><strong>$30</strong><span>one payment</span></div>
          <ul>
            <li>Unlocks Pro permanently</li>
            <li>Includes 3,000 credits</li>
            <li>Every model the router can reach</li>
            <li>Priority render queue</li>
          </ul>
          <a class="btn btn-primary btn-full" href="/signup" data-link>Start Creating</a>
        </article>
      </div>
    </div>
  </section>

  <section class="pub-section">
    <div class="pub-inner">
      <span class="pub-eyebrow">What a shot costs</span>
      <h2 class="pub-h2">Priced per operation, quoted before you spend.</h2>
      <p class="pub-lede">The workspace shows the estimate on the action bar before you press Generate, and the real settled cost afterwards.</p>
      <div class="pub-table-wrap">
        <table class="pub-table">
          <thead><tr><th>Operation</th><th>What you get</th><th>Typical</th></tr></thead>
          <tbody>
            <tr><td><strong>Frame render</strong></td><td><span>One HD still, references bound</span></td><td><span class="mono">~8 credits</span></td></tr>
            <tr><td><strong>Character identity lock</strong></td><td><span>Multi-angle reference extracted and version-locked</span></td><td><span class="mono">~6 credits</span></td></tr>
            <tr><td><strong>Full shot breakdown</strong></td><td><span>Script compiled into an ordered shot list</span></td><td><span class="mono">~20 credits</span></td></tr>
            <tr><td><strong>Video shot</strong></td><td><span>Depends on model, duration and resolution</span></td><td><span class="mono">quoted live</span></td></tr>
          </tbody>
        </table>
      </div>
      <p class="pub-price-note">Payment settles in native USDC on Base through DePay. Credits are added the moment the payment is confirmed.</p>
    </div>
  </section>

  <section class="pub-cta">
    <h2>Your next shot starts here.</h2>
    <p>Start on free credits. Top up only when you have something worth finishing.</p>
    <div class="btn-row"><a class="btn btn-primary" href="/signup" data-link>Start Creating</a></div>
  </section>`;
}

const PAGES = { "/": landing, "/product": productPage, "/models": modelsPage, "/pricing": pricingPage };

/* ---------------------------------------------------------------
   Demo: walk the mock workspace through the six production steps.
   No video, no fake footage — it animates the real state vocabulary.
   --------------------------------------------------------------- */
let demoTimer = null;
const DEMO = [
  { caption: "SCRIPT · PARSING SCENE 01", model: "—", duration: "—", cta: "Break into shots" },
  { caption: "SCENE 01 · EXT. AWNING — NIGHT", model: "—", duration: "—", cta: "Build shot list" },
  { caption: "SHOT 01 · 9:16 · READY", model: "Auto · Veo", duration: "4.0s", cta: "Generate shot" },
  { caption: "SHOT 01 · 9:16 · GENERATING", model: "Veo", duration: "4.0s", cta: "Generating…" },
  { caption: "SHOT 01 · 3 VARIANTS · REVIEW", model: "Veo", duration: "4.0s", cta: "Review variants" },
  { caption: "SHOT 01 · APPROVED — IN TIMELINE", model: "Veo", duration: "4.0s", cta: "Approved" },
];

function stopDemo() {
  window.clearInterval(demoTimer);
  demoTimer = null;
}

function runDemo() {
  const caption = document.getElementById("pubShotCaption");
  if (!caption) return;
  stopDemo();
  document.getElementById("pubShot")?.scrollIntoView({ behavior: "smooth", block: "center" });
  let step = 0;
  const paint = () => {
    const frame = DEMO[step];
    caption.textContent = frame.caption;
    const model = document.getElementById("pubShotModel");
    const duration = document.getElementById("pubShotDuration");
    const cta = document.getElementById("pubShotCta");
    if (model) model.textContent = frame.model;
    if (duration) duration.textContent = frame.duration;
    if (cta) cta.textContent = frame.cta;
    document.querySelectorAll(".pub-flow-step").forEach((node, index) => {
      node.style.background = index === step ? "var(--bg-4)" : "";
    });
    step += 1;
    if (step >= DEMO.length) {
      window.setTimeout(() => {
        document.querySelectorAll(".pub-flow-step").forEach((node) => { node.style.background = ""; });
      }, 1400);
      stopDemo();
    }
  };
  paint();
  demoTimer = window.setInterval(paint, 1500);
}

/* --------------------------------------------------------------- */

function syncHeaderForUser() {
  const user = currentUser();
  const actions = document.querySelector(".pub-actions");
  if (!actions) return;
  actions.innerHTML = user
    ? '<a class="btn btn-primary" href="/app" data-link>Open workspace</a>'
    : '<a class="btn btn-tertiary" href="/login" data-link>Sign in</a>'
      + '<a class="btn btn-primary" href="/signup" data-link>Start Creating</a>';
}

function render(route) {
  const host = mount();
  const authGate = document.getElementById("authGate");
  const footer = document.getElementById("publicFooter");
  const onAuth = AUTH_ROUTES.includes(route);

  stopDemo();
  syncHeaderForUser();

  authGate.hidden = !onAuth;
  footer.hidden = onAuth;
  host.hidden = onAuth;
  if (onAuth) {
    host.innerHTML = "";
    // app.js owns the form; it listens for this to set login vs register copy.
    window.dispatchEvent(new CustomEvent("bestshiny:auth-route", { detail: { route } }));
    return;
  }

  host.innerHTML = (PAGES[route] || landing)();
  document.getElementById("watchDemoBtn")?.addEventListener("click", runDemo);
}

onRoute((route) => {
  if (route === "/app") { stopDemo(); return; }
  render(route);
});

document.getElementById("pubMenuBtn")?.addEventListener("click", (event) => {
  const header = event.currentTarget.closest(".pub-header");
  const open = header.classList.toggle("open");
  event.currentTarget.setAttribute("aria-expanded", String(open));
});

document.addEventListener("click", (event) => {
  if (event.target.closest("a[data-link]")) {
    document.querySelector(".pub-header")?.classList.remove("open");
  }
});

export { navigate };
