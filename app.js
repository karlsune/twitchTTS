"use strict";

/* Twitch TTS overlay client.
 *
 * Responsibilities:
 *   - Play spoken chat via server neural TTS or the browser Web Speech API.
 *   - Subscribe to the SSE chat stream (chat lines, logs, voices, settings).
 *   - Drive the admin panel: live TTS rules, user overrides, global toggles.
 */

// --------------------------------------------------------------------------- //
// Elements
// --------------------------------------------------------------------------- //

const $ = (id) => document.getElementById(id);

const chatLine = $("chatLine");
const engineLog = $("engineLog");
const muteBtn = $("muteBtn");
const volumeSlider = $("volumeSlider");
const volumeValue = $("volumeValue");
const ttsModeSelect = $("ttsMode");
const browserVoiceSelect = $("browserVoice");
const browserVoiceWrap = $("browserVoiceWrap");
const enableOverlay = $("enableOverlay");
const enableAudioBtn = $("enableAudioBtn");

const adminConnState = $("adminConnState");

// Same-origin base; the page is served by the TTS server.
const SERVER_ORIGIN = window.location.origin.startsWith("http")
    ? window.location.origin
    : "http://localhost:8080";

let adminToken = "";

// --------------------------------------------------------------------------- //
// Playback state + persisted settings
// --------------------------------------------------------------------------- //

const speechQueue = [];
let isSpeaking = false;
let isMuted = false;
let audioEnabled = false;
let currentAudio = null;

const localSettings = {
    muted: localStorage.getItem("ttsMuted") === "true",
    volume: Number(localStorage.getItem("ttsVolume") ?? 80),
    mode: localStorage.getItem("ttsMode") ?? "server",
    browserVoice: localStorage.getItem("ttsBrowserVoice") ?? "",
};

isMuted = localSettings.muted;
volumeSlider.value = localSettings.volume;
ttsModeSelect.value = localSettings.mode;

function saveLocalSettings() {
    localStorage.setItem("ttsMuted", String(isMuted));
    localStorage.setItem("ttsVolume", volumeSlider.value);
    localStorage.setItem("ttsMode", ttsModeSelect.value);
    localStorage.setItem("ttsBrowserVoice", browserVoiceSelect.value);
}

function getVolume() {
    return Number(volumeSlider.value) / 100;
}

function updateMuteButton() {
    muteBtn.querySelector(".btn-label").textContent = isMuted ? "Unmute" : "Mute";
    muteBtn.querySelector(".btn-icon").textContent = isMuted ? "🔇" : "🔊";
    muteBtn.classList.toggle("muted", isMuted);
}

function updateVolumeOutput() {
    volumeValue.textContent = `${volumeSlider.value}%`;
}

function updateVoiceVisibility() {
    // Browser voice picker is only relevant in Browser engine mode. The neural
    // (server) voice is chosen once in the admin panel's default-voice setting.
    const serverMode = ttsModeSelect.value === "server";
    browserVoiceWrap.style.display = serverMode ? "none" : "";
}

// --------------------------------------------------------------------------- //
// Engine log
// --------------------------------------------------------------------------- //

function appendEngineLog(entry) {
    const line = document.createElement("div");
    line.className = `log-line log-${entry.level || "info"}`;
    line.textContent = `[${entry.time}] ${entry.message}`;
    engineLog.appendChild(line);
    engineLog.scrollTop = engineLog.scrollHeight;
    while (engineLog.children.length > 50) {
        engineLog.removeChild(engineLog.firstChild);
    }
}

function logClient(message, level = "info") {
    appendEngineLog({ time: new Date().toLocaleTimeString(), level, message });
}

// --------------------------------------------------------------------------- //
// Voice lists
// --------------------------------------------------------------------------- //

let serverVoicesCache = [];

function populateVoiceSelect(select, voices, preferred) {
    const previous = select.value;
    select.innerHTML = "";
    voices.forEach((voice) => {
        const option = document.createElement("option");
        option.value = voice.name;
        option.textContent = voice.label;
        select.appendChild(option);
    });
    const target = preferred || previous;
    if (target) select.value = target;
    if (!select.value && select.options.length > 0) select.selectedIndex = 0;
}

function populateServerVoices(voices) {
    const english = voices.filter((v) => v.locale.startsWith("en-"));
    serverVoicesCache = english.length > 0 ? english : voices;
    // The admin "default voice" picker is the single source for the neural voice.
    populateVoiceSelect(setVoice, serverVoicesCache, adminSettings.tts_voice);
}

async function loadServerVoices() {
    try {
        const response = await fetch(`${SERVER_ORIGIN}/api/voices`, { cache: "no-store" });
        if (!response.ok) throw new Error(`Voice list failed (${response.status})`);
        populateServerVoices(await response.json());
    } catch (error) {
        logClient(`Voice API unavailable (${error.message}). Waiting for SSE voice list…`, "warn");
    }
}

function loadBrowserVoices() {
    const voices = window.speechSynthesis.getVoices();
    browserVoiceSelect.innerHTML = "";
    voices.forEach((voice) => {
        const option = document.createElement("option");
        option.value = voice.name;
        option.textContent = `${voice.name} (${voice.lang})`;
        browserVoiceSelect.appendChild(option);
    });
    if (localSettings.browserVoice) browserVoiceSelect.value = localSettings.browserVoice;
}

// --------------------------------------------------------------------------- //
// Audio unlock + playback
// --------------------------------------------------------------------------- //

async function unlockAudio() {
    audioEnabled = true;
    enableOverlay.classList.add("hidden");
    const silent = new Audio(
        "data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYYoRwmHAAAAAAD/+1DEAAAHAAGf9AAAIAAANIAAAAQAAAaAAAAD/+1DEAg/wAABpAAAACAAAD/AAAA"
    );
    silent.volume = 0;
    try {
        await silent.play();
    } catch (error) {
        console.warn("Silent unlock failed:", error);
    }
    processQueue();
}

function finishSpeaking() {
    isSpeaking = false;
    currentAudio = null;
    processQueue();
}

async function speakWithServer(text) {
    // Use the server-side default neural voice (edited in the admin panel).
    const voice = encodeURIComponent(adminSettings.tts_voice || "");
    const response = await fetch(
        `${SERVER_ORIGIN}/api/tts?text=${encodeURIComponent(text)}&voice=${voice}`
    );
    if (!response.ok) throw new Error(`Server TTS failed (${response.status})`);

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.volume = getVolume();
    currentAudio = audio;

    await new Promise((resolve, reject) => {
        audio.onended = () => { URL.revokeObjectURL(url); resolve(); };
        audio.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Audio playback failed")); };
        audio.play().catch(reject);
    });
}

function speakWithBrowser(text) {
    return new Promise((resolve, reject) => {
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.rate = 1.0;
        utterance.pitch = 1.0;
        utterance.volume = getVolume();
        const selectedVoice = window.speechSynthesis
            .getVoices()
            .find((voice) => voice.name === browserVoiceSelect.value);
        if (selectedVoice) utterance.voice = selectedVoice;
        utterance.onend = resolve;
        utterance.onerror = () => reject(new Error("Browser TTS failed"));
        window.speechSynthesis.speak(utterance);
    });
}

async function processQueue() {
    if (!audioEnabled || isSpeaking || isMuted || speechQueue.length === 0) return;

    isSpeaking = true;
    const messageText = speechQueue.shift();

    try {
        if (ttsModeSelect.value === "server") {
            await speakWithServer(messageText);
        } else {
            await speakWithBrowser(messageText);
        }
    } catch (error) {
        logClient(`Playback issue: ${error.message}`, "warn");
        if (ttsModeSelect.value === "server") {
            try {
                await speakWithBrowser(messageText);
            } catch (fallbackError) {
                logClient(`Fallback TTS failed: ${fallbackError.message}`, "error");
            }
        }
    } finally {
        finishSpeaking();
    }
}

// --------------------------------------------------------------------------- //
// SSE stream
// --------------------------------------------------------------------------- //

function connectStream() {
    const streamOrigin = `http://${window.location.hostname || "localhost"}:8081`;
    const eventSource = new EventSource(streamOrigin);

    eventSource.onopen = () => setConnState(true);

    eventSource.onmessage = (event) => {
        if (event.data.startsWith(":")) return;
        const data = JSON.parse(event.data);

        if (data.type === "log") { appendEngineLog(data); return; }
        if (data.type === "voices") { populateServerVoices(data.voices); return; }
        if (data.type === "settings") { applyServerSettings(data.settings); return; }
        if (data.type === "shutdown") { handleServerShutdown(); return; }

        chatLine.innerHTML = "";
        const userSpan = document.createElement("span");
        userSpan.className = "chat-user";
        userSpan.textContent = data.user;
        const textSpan = document.createElement("span");
        textSpan.className = "chat-text";
        textSpan.textContent = data.text;
        chatLine.append(userSpan, document.createTextNode(" "), textSpan);

        if (!isMuted && data.text) {
            speechQueue.push(data.text);
            processQueue();
        }
    };

    eventSource.onerror = () => {
        setConnState(false);
        logClient("Connection to core engine broken. Retrying…", "warn");
    };
}

function handleServerShutdown() {
    logClient("Engine shut down. Closing overlay…", "warn");
    speechQueue.length = 0;
    window.speechSynthesis.cancel();
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    // Try to close the tab (works when opened by script/OBS); otherwise blank it.
    setTimeout(() => {
        window.close();
        document.body.innerHTML =
            '<div class="shutdown-notice">TTS engine stopped. You can close this page.</div>';
    }, 400);
}

function setConnState(connected) {
    adminConnState.textContent = connected ? "Connected" : "Reconnecting…";
    adminConnState.classList.toggle("pill--ok", connected);
    adminConnState.classList.toggle("pill--muted", !connected);
}

// --------------------------------------------------------------------------- //
// Admin: elements + state
// --------------------------------------------------------------------------- //

const setMode = $("setMode");
const setCommand = $("setCommand");
const setPermission = $("setPermission");
const setCooldown = $("setCooldown");
const setVoice = $("setVoice");
const setMaxChars = $("setMaxChars");
const settingsSave = $("settingsSave");
const settingsReset = $("settingsReset");
const settingsFeedback = $("settingsFeedback");

const adminUser = $("adminUser");
const adminGroup = $("adminGroup");
const forceAllToggle = $("forceAllToggle");

// Last known server-side settings (source of truth for reset + reconcile).
let adminSettings = {
    tts_mode: "all",
    tts_command: "!tts",
    tts_permission: "everyone",
    tts_cooldown_seconds: 0,
    tts_voice: "en-US-JennyNeural",
    max_tts_chars: 200,
};

function fillSettingsForm(s) {
    setMode.value = s.tts_mode;
    setCommand.value = s.tts_command;
    setPermission.value = s.tts_permission;
    setCooldown.value = s.tts_cooldown_seconds;
    setMaxChars.value = s.max_tts_chars;
    if (s.tts_voice) populateVoiceSelect(setVoice, serverVoicesCache, s.tts_voice);
    updateModeFields();
}

function applyServerSettings(s) {
    adminSettings = { ...adminSettings, ...s };
    fillSettingsForm(adminSettings);
}

function updateModeFields() {
    const commandMode = setMode.value === "command";
    document.querySelectorAll('[data-mode="command"]').forEach((el) => {
        el.classList.toggle("disabled", !commandMode);
    });
}

function showFeedback(message, level = "info") {
    settingsFeedback.textContent = message;
    settingsFeedback.className = `feedback feedback--${level}`;
    if (level === "ok") {
        setTimeout(() => {
            if (settingsFeedback.textContent === message) {
                settingsFeedback.textContent = "";
                settingsFeedback.className = "feedback";
            }
        }, 3000);
    }
}

// --------------------------------------------------------------------------- //
// Admin: API
// --------------------------------------------------------------------------- //

function currentToken() {
    return $("adminToken").value.trim() || adminToken;
}

async function loadAdminToken() {
    try {
        const response = await fetch(`${SERVER_ORIGIN}/api/config`, { cache: "no-store" });
        if (response.ok) {
            const data = await response.json();
            adminToken = data.admin_token || "";
            const field = $("adminToken");
            if (field && adminToken && !field.value) field.value = adminToken;
        }
    } catch (error) {
        /* Admin panel still works if the operator pastes a token manually. */
    }
}

async function fetchServerSettings() {
    try {
        const query = new URLSearchParams({ token: currentToken() });
        const response = await fetch(`${SERVER_ORIGIN}/api/settings?${query}`, { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "Failed to load settings");
        applyServerSettings(payload.settings);
    } catch (error) {
        showFeedback(error.message, "warn");
    }
}

async function saveServerSettings() {
    const updates = {
        tts_mode: setMode.value,
        tts_command: setCommand.value.trim(),
        tts_permission: setPermission.value,
        tts_cooldown_seconds: Number(setCooldown.value || 0),
        tts_voice: setVoice.value,
        max_tts_chars: Number(setMaxChars.value || 200),
    };
    settingsSave.disabled = true;
    try {
        const response = await fetch(`${SERVER_ORIGIN}/api/settings`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            cache: "no-store",
            body: JSON.stringify({ token: currentToken(), settings: updates }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.message || "Save failed");
        applyServerSettings(payload.settings);
        showFeedback("Saved ✓", "ok");
    } catch (error) {
        showFeedback(error.message, "warn");
    } finally {
        settingsSave.disabled = false;
    }
}

async function sendAdminRequest(params) {
    const token = currentToken();
    if (token) params.token = token;
    const query = new URLSearchParams(params);
    const response = await fetch(`${SERVER_ORIGIN}/api/admin?${query}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || "Admin request failed");
    return payload;
}

// --------------------------------------------------------------------------- //
// Admin: user overrides + status
// --------------------------------------------------------------------------- //

function renderList(ulId, users, chipClass = "chip") {
    const ul = $(ulId);
    ul.innerHTML = "";
    if (!users || users.length === 0) {
        const empty = document.createElement("li");
        empty.className = "chips-empty";
        empty.textContent = "none";
        ul.appendChild(empty);
        return;
    }
    users.forEach((user) => {
        const li = document.createElement("li");
        li.className = chipClass;
        li.textContent = user;
        ul.appendChild(li);
    });
}

function applyAdminStatus(status) {
    if (!status) return;
    renderList("listCommandless", status.allowed_without_command);
    renderList("listNocooldown", status.allowed_without_cooldown);
    renderList("listBlacklist", status.blacklist, "chip chip--danger");
    const on = Boolean(status.broadcaster_force_all_mode);
    forceAllToggle.setAttribute("aria-checked", String(on));
    forceAllToggle.classList.toggle("on", on);
}

async function refreshAdminStatus() {
    try {
        const result = await sendAdminRequest({ action: "status" });
        applyAdminStatus(result.status);
    } catch (error) {
        showFeedback(error.message, "warn");
    }
}

// --------------------------------------------------------------------------- //
// Event wiring
// --------------------------------------------------------------------------- //

enableAudioBtn.addEventListener("click", unlockAudio);

muteBtn.addEventListener("click", () => {
    isMuted = !isMuted;
    updateMuteButton();
    if (isMuted) {
        speechQueue.length = 0;
        window.speechSynthesis.cancel();
        if (currentAudio) { currentAudio.pause(); currentAudio = null; }
        isSpeaking = false;
    }
    saveLocalSettings();
});

volumeSlider.addEventListener("input", () => {
    if (currentAudio) currentAudio.volume = getVolume();
    updateVolumeOutput();
    saveLocalSettings();
});

ttsModeSelect.addEventListener("change", () => {
    updateVoiceVisibility();
    saveLocalSettings();
});

browserVoiceSelect.addEventListener("change", saveLocalSettings);

// Settings form
setMode.addEventListener("change", updateModeFields);
settingsSave.addEventListener("click", saveServerSettings);
settingsReset.addEventListener("click", () => { fillSettingsForm(adminSettings); showFeedback("Reverted to saved values"); });

// User overrides
$("adminAllow").addEventListener("click", async () => {
    const user = adminUser.value.trim();
    if (!user) return showFeedback("Enter a username first.", "warn");
    try {
        await sendAdminRequest({ action: "allow", group: adminGroup.value, user });
        adminUser.value = "";
        refreshAdminStatus();
    } catch (error) { showFeedback(error.message, "warn"); }
});

$("adminDisallow").addEventListener("click", async () => {
    const user = adminUser.value.trim();
    if (!user) return showFeedback("Enter a username first.", "warn");
    try {
        await sendAdminRequest({ action: "disallow", group: adminGroup.value, user });
        adminUser.value = "";
        refreshAdminStatus();
    } catch (error) { showFeedback(error.message, "warn"); }
});

// Force-all toggle
forceAllToggle.addEventListener("click", async () => {
    const turningOn = forceAllToggle.getAttribute("aria-checked") !== "true";
    try {
        await sendAdminRequest({ action: turningOn ? "enable_all" : "disable_all" });
        refreshAdminStatus();
    } catch (error) { showFeedback(error.message, "warn"); }
});

// Shutdown
$("adminShutdown").addEventListener("click", async () => {
    if (!window.confirm("Shut down the TTS engine?")) return;
    try {
        const result = await sendAdminRequest({ action: "shutdown" });
        showFeedback(result.message, "info");
    } catch (error) { showFeedback(error.message, "warn"); }
});

// Log collapse
$("logToggle").addEventListener("click", () => {
    const wrap = $("engineLogWrap");
    const collapsed = wrap.classList.toggle("collapsed");
    $("logToggle").setAttribute("aria-expanded", String(!collapsed));
});

// --------------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------------- //

window.speechSynthesis.onvoiceschanged = loadBrowserVoices;
loadBrowserVoices();
loadServerVoices();
updateMuteButton();
updateVolumeOutput();
updateVoiceVisibility();
updateModeFields();
connectStream();
loadAdminToken().then(() => {
    refreshAdminStatus();
    fetchServerSettings();
});
