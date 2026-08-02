/**
 * 语音对话组件 v2 - 自动监听版
 *
 * 特性：
 * - 自动监听：启动后持续听，无需点击
 * - 唤醒词：听到"白泽"后进入指令模式（可开关）
 * - 回声消除：白泽播报时暂停识别，播报完恢复
 * - 连续对话：播报完毕自动继续听
 */
const VoiceComponent = {
    recognition: null,
    autoListen: true,       // 自动监听（默认开）
    wakeWord: '白泽',        // 唤醒词
    wakeWordEnabled: true,   // 唤醒词门控（开则需先说"白泽"再说指令）
    armed: false,           // 已唤醒，等待指令
    ttsEnabled: true,
    isSpeaking: false,      // 白泽正在播报（此时暂停ASR）
    restartTimer: null,
    supported: false,

    init() {
        this.micBtn = document.getElementById('voice-btn');
        this.autoBtn = document.getElementById('voice-auto-btn');
        this.ttsBtn = document.getElementById('voice-tts-btn');
        this.statusEl = document.getElementById('voice-status');

        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) {
            this.log('当前浏览器不支持语音识别，请用 Chrome/Edge');
            return;
        }
        this.supported = true;

        this.recognition = new SR();
        this.recognition.lang = 'zh-CN';
        this.recognition.continuous = true;       // 持续监听
        this.recognition.interimResults = true;
        this.recognition.maxAlternatives = 1;

        this.recognition.onresult = (e) => this.handleResult(e);
        this.recognition.onend = () => this.handleEnd();
        this.recognition.onerror = (e) => this.handleError(e);
        this.recognition.onstart = () => { this.isListening = true; this.updateUI(); };

        this.bindEvents();
        this.log('🎤 自动监听已就绪');
        this.log(this.wakeWordEnabled ? `说"${this.wakeWord}"唤醒，然后说指令` : '直接说话即可');

        // 自动启动监听
        if (this.autoListen) {
            setTimeout(() => this.start(), 500);
        }
    },

    bindEvents() {
        if (this.micBtn) this.micBtn.addEventListener('click', () => this.isListening ? this.stop() : this.start());
        if (this.autoBtn) this.autoBtn.addEventListener('click', () => { this.autoListen = !this.autoListen; if (this.autoListen && !this.isListening) this.start(); this.updateUI(); });
        if (this.ttsBtn) this.ttsBtn.addEventListener('click', () => { this.ttsEnabled = !this.ttsEnabled; if (!this.ttsEnabled) window.speechSynthesis?.cancel(); this.updateUI(); });
    },

    start() {
        if (!this.supported || this.isListening || this.isSpeaking) return;
        try { this.recognition.start(); this.isListening = true; this.updateUI(); }
        catch (e) { /* 可能已启动 */ }
    },

    stop() {
        if (!this.supported || !this.isListening) return;
        clearTimeout(this.restartTimer);
        this.autoListen = false;  // 手动停止后不再自动恢复
        try { this.recognition.stop(); } catch (e) {}
    },

    handleResult(e) {
        let interim = '', final = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
            const t = e.results[i][0].transcript;
            if (e.results[i].isFinal) final += t; else interim += t;
        }
        const input = document.getElementById('message-input');
        if (input) input.value = final || interim;

        if (!final) return;
        final = final.trim();
        this.log(`听到: ${final}`);

        if (this.wakeWordEnabled && !this.armed) {
            // 等待唤醒词
            if (final.includes(this.wakeWord)) {
                this.armed = true;
                this.log('✅ 已唤醒，请说指令...');
                // 唤醒后清空输入，等待指令
                if (input) input.value = '';
                // 提示音/语音
                this.speak('嗯，在的');
            }
            return;
        }

        // 已唤醒或无需唤醒词 → 当作指令发送
        this.armed = false;
        this.sendMessage(final);
    },

    handleEnd() {
        this.isListening = false;
        this.updateUI();
        // 自动监听模式下，播报结束后自动恢复监听
        if (this.autoListen && !this.isSpeaking) {
            // 浏览器会因停顿自动 end，重启继续听
            this.restartTimer = setTimeout(() => this.start(), 200);
        }
    },

    handleError(e) {
        this.isListening = false;
        this.updateUI();
        if (e.error === 'not-allowed') {
            this.log('⚠️ 请允许麦克风权限');
            this.autoListen = false;
        } else if (e.error === 'no-speech' || e.error === 'aborted') {
            // 正常的无语音/中断，自动恢复
            if (this.autoListen && !this.isSpeaking) {
                this.restartTimer = setTimeout(() => this.start(), 300);
            }
        } else {
            this.log(`识别错误: ${e.error}`);
            if (this.autoListen) this.restartTimer = setTimeout(() => this.start(), 1000);
        }
    },

    sendMessage(text) {
        const input = document.getElementById('message-input');
        if (input) input.value = text;
        if (window.ChatComponent?.sendMessage) ChatComponent.sendMessage();
    },

    /** 语音合成 - 播报（播报时暂停识别避免回声） */
    speak(text) {
        if (!this.ttsEnabled || !('speechSynthesis' in window)) return;
        window.speechSynthesis.cancel();
        // 暂停 ASR，避免把播报声识别进去
        this.isSpeaking = true;
        try { if (this.isListening) this.recognition.stop(); } catch (e) {}

        const clean = text.replace(/[#*`_>\[\]]/g, '').replace(/\n+/g, '。').replace(/```[\s\S]*?```/g, '').trim();
        const utter = new SpeechSynthesisUtterance(clean);
        utter.lang = 'zh-CN';
        utter.rate = 1.05;
        const voices = window.speechSynthesis.getVoices();
        const zh = voices.find(v => v.lang.startsWith('zh'));
        if (zh) utter.voice = zh;
        utter.onend = () => {
            this.isSpeaking = false;
            // 播报完毕，恢复监听
            if (this.autoListen) {
                this.armed = this.wakeWordEnabled; // 唤醒词模式：播报后回到待唤醒
                this.restartTimer = setTimeout(() => this.start(), 300);
            }
        };
        utter.onerror = () => { this.isSpeaking = false; };
        window.speechSynthesis.speak(utter);
        this.log('🔊 播报中...');
    },

    log(msg) {
        if (this.statusEl) this.statusEl.textContent = msg;
        console.log('[voice]', msg);
    },

    updateUI() {
        if (this.micBtn) {
            this.micBtn.textContent = this.isListening ? '🔴 监听中' : '🎤 开始监听';
            this.micBtn.classList.toggle('listening', this.isListening);
        }
        if (this.autoBtn) {
            this.autoBtn.textContent = this.autoListen ? '🔁 自动监听:开' : '🔁 自动监听:关';
            this.autoBtn.classList.toggle('active', this.autoListen);
        }
        if (this.ttsBtn) {
            this.ttsBtn.textContent = this.ttsEnabled ? '🔊 播报:开' : '🔇 播报:关';
            this.ttsBtn.classList.toggle('active', this.ttsEnabled);
        }
    },

    isListening: false,
};

if ('speechSynthesis' in window) {
    window.speechSynthesis.onvoiceschanged = () => VoiceComponent.supported && void 0;
}
