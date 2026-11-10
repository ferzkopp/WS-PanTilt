"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const byId = id => document.getElementById(id);
let pending = 0;
let state = null;
let shownVersion = 0;
let oledInitialized = false;
let apInitialized = false;
lucide.createIcons();

function message(text, error = false) {
  byId("message").textContent = text;
  byId("message").classList.toggle("error", error);
}
function angle(value) { return value == null ? "--" : `${Number(value).toFixed(1)}°`; }
function render(data) {
  state = data;
  byId("commanded-pan").textContent = angle(data.commanded_pan);
  byId("commanded-tilt").textContent = angle(data.commanded_tilt);
  byId("measured-pan").textContent = angle(data.measured_pan);
  byId("measured-tilt").textContent = angle(data.measured_tilt);
  byId("voltage").textContent = data.voltage == null ? "--" : `${data.voltage.toFixed(2)} V`;
  byId("led").checked = data.led;
  byId("brightness").value = data.brightness;
  byId("brightness-value").value = data.brightness;
  byId("connection").textContent = data.feedback_error ? "Feedback unavailable" : (data.demo ? "Simulation" : "Connected");
  byId("connection").classList.toggle("offline", Boolean(data.feedback_error));
  const feedbackTime = data.feedback_at ? `Last telemetry: ${new Date(data.feedback_at).toLocaleTimeString()}` : "No fresh telemetry";
  for (const id of ["connection", "measured-pan", "measured-tilt"]) byId(id).title = feedbackTime;
  byId("firmware").textContent = data.firmware_version ? `FW ${data.firmware_version}` : "FW --";
  byId("firmware").title = data.firmware_source || "Version not reported";
  for (const cell of document.querySelectorAll("[data-imu]")) {
    const value = data.imu?.[cell.dataset.imu];
    cell.textContent = value == null ? "--" : Number(value).toFixed(Number(cell.dataset.precision ?? 2));
  }
  byId("sensor-time").textContent = data.imu_error ? "Sensor reply unavailable" : data.imu_at ? new Date(data.imu_at).toLocaleTimeString() : "--";
  if (!oledInitialized) { byId("oled-mode").value = data.oled_mode; oledInitialized = true; }
  if (byId("oled-mode").value !== "custom") {
    (data.oled_lines || []).forEach((text, index) => { byId(`oled-${index}`).value = text; });
  }
  byId("ap-ssid").textContent = data.ap?.ssid || "Not reported";
  byId("ap-current-password").placeholder = data.ap?.password_set ? "Stored" : "Not reported";
  if (!apInitialized && data.ap?.ssid) { byId("ap-ssid-input").value = data.ap.ssid; apInitialized = true; }
  syncSettings();
  if (data.snapshot_at) byId("snapshot-time").textContent = new Date(data.snapshot_at).toLocaleTimeString();
  if (data.snapshot_version > shownVersion) {
    const image = byId("camera-image");
    image.onload = () => { image.hidden = false; byId("camera-empty").hidden = true; byId("download").hidden = false; };
    image.onerror = () => message("Could not load snapshot", true);
    image.src = `/api/image?v=${data.snapshot_version}`;
    shownVersion = data.snapshot_version;
  }
}
function syncSettings() {
  for (let index=0; index<4; index++) byId(`oled-${index}`).disabled = pending > 0 || byId("oled-mode").value !== "custom";
  byId("ap-apply").disabled = pending > 0 || !byId("ap-confirm").checked;
}
async function api(action, data = {}) {
  const response = await fetch(`/api/${action}`, {method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":csrf}, body:JSON.stringify(data)});
  if (response.status === 401) { location.assign("/login"); throw new Error("Sign in required"); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Command failed");
  return result;
}
async function refresh() {
  if (pending) return;
  try {
    const response = await fetch("/api/state");
    if (response.status === 401) { location.assign("/login"); return; }
    if (!response.ok) throw new Error("Connection unavailable");
    render(await response.json());
  } catch (error) { byId("connection").textContent = "Disconnected"; byId("connection").classList.add("offline"); }
}
async function command(action, data = {}) {
  if (pending && action !== "stop") return;
  pending += 1;
  document.querySelectorAll("button:not(#stop), input, select, #download").forEach(element => { element.disabled = true; });
  try {
    const result = await api(action, data);
    render(result);
    message(action === "snapshot" ? "Snapshot captured" : action === "stop" ? "Movement stopped" : "Command sent");
  } catch (error) { message(error.message, true); if (state) render(state); }
  finally { pending -= 1; document.querySelectorAll("button:not(#stop), input, select, #download").forEach(element => { element.disabled = pending > 0; }); syncSettings(); }
}
for (const axis of ["pan", "tilt", "speed"]) {
  byId(axis).addEventListener("input", () => { byId(`${axis}-value`).value = byId(axis).value; });
  byId(`${axis}-value`).addEventListener("input", () => { byId(axis).value = byId(`${axis}-value`).value; });
}
byId("position-form").addEventListener("submit", event => {
  event.preventDefault();
  command("move", {pan: Number(byId("pan-value").value), tilt: Number(byId("tilt-value").value), speed: Number(byId("speed-value").value)});
});
byId("home").addEventListener("click", async () => {
  await command("home", {speed: Number(byId("speed-value").value)});
  if (state?.commanded_pan === 0 && state?.commanded_tilt === 0) {
    for (const axis of ["pan", "tilt"]) { byId(axis).value = 0; byId(`${axis}-value`).value = 0; }
  }
});
byId("stop").addEventListener("click", () => command("stop"));
byId("snapshot").addEventListener("click", async () => {
  byId("capture-state").textContent = "Capturing";
  await command("snapshot");
  byId("capture-state").textContent = "";
});
byId("led").addEventListener("change", () => command("led", {enabled: byId("led").checked, brightness: Number(byId("brightness").value)}));
byId("brightness").addEventListener("input", () => { byId("brightness-value").value = byId("brightness").value; });
byId("brightness").addEventListener("change", () => command("led", {enabled: byId("led").checked, brightness: Number(byId("brightness").value)}));
byId("logout").addEventListener("click", async () => {
  byId("ap-current-password").value = "";
  await fetch("/api/logout", {method: "POST", headers: {"X-CSRF-Token": csrf}});
  location.assign("/login");
});
byId("oled-mode").addEventListener("change", syncSettings);
byId("oled-form").addEventListener("submit", event => {
  event.preventDefault();
  command("oled", {mode:byId("oled-mode").value, lines:Array.from({length:4},(_,index)=>byId(`oled-${index}`).value)});
});
byId("ap-refresh").addEventListener("click", () => command("ap-refresh"));
byId("ap-confirm").addEventListener("change", syncSettings);
byId("ap-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (byId("ap-password").value !== byId("ap-password-confirm").value) { message("New passwords do not match",true); return; }
  message("Saving ESP32 AP settings");
  await command("ap-set", {ssid:byId("ap-ssid-input").value,password:byId("ap-password").value,password_confirm:byId("ap-password-confirm").value,confirmed:byId("ap-confirm").checked});
  byId("ap-password").value = ""; byId("ap-password-confirm").value = "";
  byId("ap-current-password").value = ""; byId("ap-current-password").type = "password";
  byId("ap-confirm").checked = false; syncSettings();
});
byId("ap-reveal").addEventListener("click", async () => {
  const field = byId("ap-current-password");
  if (field.type === "text") { field.value = ""; field.type = "password"; return; }
  byId("ap-reveal").disabled = true;
  try { const result=await api("ap-reveal"); field.value=result.password; field.type="text"; }
  catch(error) { message(error.message,true); }
  finally { byId("ap-reveal").disabled = false; }
});
refresh();
setInterval(refresh, 1000);