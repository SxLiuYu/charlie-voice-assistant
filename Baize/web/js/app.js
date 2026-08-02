/**
 * 白泽3.0 Web 前端主应用
 */

const App = {
    /**
     * 初始化
     */
    init() {
        this.bindEvents();
        this.initComponents();
        this.testConnection();
    },

    /**
     * 绑定事件
     */
    bindEvents() {
        // 导航切换
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                const page = e.currentTarget.dataset.page;
                this.switchPage(page);
            });
        });

        // 模态框关闭
        document.getElementById('modal-close').addEventListener('click', () => {
            Utils.hideModal();
        });

        // 点击模态框外部关闭
        document.getElementById('modal').addEventListener('click', (e) => {
            if (e.target.id === 'modal') {
                Utils.hideModal();
            }
        });

        // ESC 关闭模态框
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                Utils.hideModal();
            }
        });
    },

    /**
     * 初始化组件
     */
    initComponents() {
        ChatComponent.init();
        SkillsComponent.init();
        MemoryComponent.init();
        CostComponent.init();
        ConfigComponent.init();
    },

    /**
     * 切换页面
     */
    switchPage(pageName) {
        // 更新导航状态
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.remove('active');
            if (item.dataset.page === pageName) {
                item.classList.add('active');
            }
        });

        // 更新页面显示
        document.querySelectorAll('.page').forEach(page => {
            page.classList.remove('active');
        });
        
        const targetPage = document.getElementById(`page-${pageName}`);
        if (targetPage) {
            targetPage.classList.add('active');
        }
    },

    /**
     * 测试连接
     */
    async testConnection() {
        // 先自动检测可用端口
        await BaizeAPI.autoDetectPort();
        
        const result = await BaizeAPI.testConnection();
        
        const statusDot = document.querySelector('#connection-status .status-dot');
        const statusText = document.querySelector('#connection-status .status-text');
        
        if (result.connected) {
            statusDot.classList.remove('offline');
            statusDot.classList.add('online');
            statusText.textContent = '已连接';
            
            // 更新版本信息
            document.getElementById('version').textContent = result.version || '-';
            
            // 更新 API 地址显示
            const apiConfig = BaizeAPI.getConfig();
            const apiUrlInput = document.getElementById('api-url');
            if (apiUrlInput && apiConfig.baseURL) {
                apiUrlInput.value = apiConfig.baseURL;
            }
        } else {
            statusDot.classList.remove('online');
            statusDot.classList.add('offline');
            statusText.textContent = '未连接';
        }
    },
};

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
    App.init();
    // 初始化语音组件
    if (window.VoiceComponent) VoiceComponent.init();
    // 主动提醒推送（JARVIS式：白泽主动开口提醒）
    initProactiveReminders();
});

/**
 * 主动提醒推送 - 监听服务端SSE，到期提醒时白泽开口播报
 */
function initProactiveReminders() {
    if (!window.EventSource) { console.warn('浏览器不支持EventSource'); return; }
    let base = 'http://localhost:3000';
    if (window.BaizeAPI && BaizeAPI.getConfig) {
        try { base = BaizeAPI.getConfig().baseURL || base; } catch {}
    }
    const connect = () => {
        const es = new EventSource(base + '/api/reminders/stream');
        es.addEventListener('reminder', (e) => {
            try {
                const data = JSON.parse(e.data);
                if (data.message) {
                    showReminderToast(data.message);
                    if (window.VoiceComponent && VoiceComponent.ttsEnabled !== false) {
                        VoiceComponent.speak(data.message);
                    }
                }
            } catch (err) { console.warn('提醒解析失败', err); }
        });
        es.addEventListener('open', () => console.log('白泽主动提醒通道已连接'));
        es.addEventListener('error', () => { es.close(); setTimeout(connect, 10000); });
    };
    connect();
}

function showReminderToast(msg) {
    let toast = document.getElementById('baize-reminder-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'baize-reminder-toast';
        toast.style.cssText = 'position:fixed;top:20px;right:20px;max-width:380px;background:linear-gradient(135deg,#1a1a2e,#16213e);color:#e94560;border:2px solid #e94560;border-radius:14px;padding:16px 20px;z-index:99999;box-shadow:0 8px 32px rgba(233,69,96,.4);font-size:15px;line-height:1.5;animation:slideIn .4s ease;display:none;';
        document.body.appendChild(toast);
    }
    toast.innerHTML = '<div style="font-weight:700;margin-bottom:6px;">⏰ 白泽提醒</div><div>' + msg.replace(/\n/g, '<br>') + '</div>';
    toast.style.display = 'block';
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.style.display = 'none'; }, 20000);
}
