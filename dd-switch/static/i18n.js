// claude-gearshift i18n — shared by login.html and index.html
// Client-side Chinese/English switching, persisted in localStorage.
(function () {
  const I18N = {
    zh: {
      title: "dd-switch - Claude Code 配置切换",
      loginTitle: "dd-switch - 验证",
      subtitle: "Claude Code 配置切换面板",
      configsUnit: (n) => `${n} 个配置`,
      logout: "退出登录",
      configFiles: "配置文件",
      addNew: "+ 新增",
      loading: "加载中...",
      noConfigs: "暂无配置文件",
      noConfigsHint: '点击右上角"新增"创建',
      invalid: "无效",
      currentConfig: "当前使用中的配置",
      notCreated: "尚未创建 settings.json",
      otherFields: "其他字段",
      selectConfig: "选择一个配置",
      selectHint: "选择左侧配置查看内容...",
      switchTo: "🔄 切换到此配置",
      save: "💾 保存",
      jsonError: "JSON 格式错误",
      confirmSwitch: "确认切换配置",
      confirmMsg: (name) => `确定要切换到「${name}」吗？当前配置将自动备份。`,
      cancel: "取消",
      confirmBtn: "确认切换",
      switching: "切换中...",
      newConfig: "新增配置",
      fileNamePlaceholder: "文件名（如 my_provider.json）",
      newConfigPlaceholder:
        '{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "",\n    "ANTHROPIC_BASE_URL": "",\n    "ANTHROPIC_MODEL": ""\n  }\n}',
      create: "创建",
      edit: "编辑",
      delete: "删除",
      loginPrompt: "请输入密码继续",
      passwordPlaceholder: "密码",
      loginBtn: "验证",
      loginError: "密码错误",
      loginExpired: "登录已过期",
      requestFailed: "请求失败",
      readFailed: "读取失败",
      switchedTo: (name) => `✅ 已切换为 ${name}`,
      saveSuccess: "💾 保存成功",
      saveJsonError: "JSON 格式错误，无法保存",
      confirmDelete: (name) => `确定删除「${name}」吗？`,
      deleted: (name) => `已删除 ${name}`,
      enterFileName: "请输入文件名",
      createSuccess: (name) => `✅ 创建成功: ${name}`,
      jsonErrorPrefix: "JSON 格式错误",
      errNotLoggedIn: "未登录",
      errConfigMissing: (name) => `配置文件 ${name} 不存在`,
      errNameContentRequired: "name 和 content 不能为空",
      errInvalidFileName: "文件名不合法",
      errFileExists: (name) => `文件 ${name} 已存在`,
      errNameRequired: "name 不能为空",
      errContentRequired: "content 不能为空",
      errSwitchFailed: (msg) => `切换失败: ${msg}`,
    },
    en: {
      title: "dd-switch - Claude Code Config Switcher",
      loginTitle: "dd-switch - Login",
      subtitle: "Claude Code Config Switcher Panel",
      configsUnit: (n) => `${n} config${n === 1 ? "" : "s"}`,
      logout: "Log out",
      configFiles: "Config Files",
      addNew: "+ New",
      loading: "Loading...",
      noConfigs: "No config files",
      noConfigsHint: 'Click "+ New" in the top right to create one',
      invalid: "Invalid",
      currentConfig: "Active Config",
      notCreated: "settings.json not created yet",
      otherFields: "Other Fields",
      selectConfig: "Select a config",
      selectHint: "Select a config on the left to view...",
      switchTo: "🔄 Switch to this config",
      save: "💾 Save",
      jsonError: "Invalid JSON",
      confirmSwitch: "Confirm Switch",
      confirmMsg: (name) => `Switch to "${name}"? The current config will be backed up automatically.`,
      cancel: "Cancel",
      confirmBtn: "Confirm",
      switching: "Switching...",
      newConfig: "New Config",
      fileNamePlaceholder: "File name (e.g. my_provider.json)",
      newConfigPlaceholder:
        '{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "",\n    "ANTHROPIC_BASE_URL": "",\n    "ANTHROPIC_MODEL": ""\n  }\n}',
      create: "Create",
      edit: "Edit",
      delete: "Delete",
      loginPrompt: "Enter password to continue",
      passwordPlaceholder: "Password",
      loginBtn: "Log in",
      loginError: "Wrong password",
      loginExpired: "Session expired",
      requestFailed: "Request failed",
      readFailed: "Failed to read",
      switchedTo: (name) => `✅ Switched to ${name}`,
      saveSuccess: "💾 Saved",
      saveJsonError: "Invalid JSON, cannot save",
      confirmDelete: (name) => `Delete "${name}"?`,
      deleted: (name) => `Deleted ${name}`,
      enterFileName: "Please enter a file name",
      createSuccess: (name) => `✅ Created: ${name}`,
      jsonErrorPrefix: "Invalid JSON",
      errNotLoggedIn: "Not logged in",
      errConfigMissing: (name) => `Config file ${name} not found`,
      errNameContentRequired: "name and content are required",
      errInvalidFileName: "Invalid file name",
      errFileExists: (name) => `File ${name} already exists`,
      errNameRequired: "name is required",
      errContentRequired: "content is required",
      errSwitchFailed: (msg) => `Switch failed: ${msg}`,
    },
  };

  function getLang() {
    return localStorage.getItem("cg_lang") || "zh";
  }
  function setLang(lang) {
    localStorage.setItem("cg_lang", lang);
  }
  function t(key, ...args) {
    const lang = getLang();
    let s = (I18N[lang] && I18N[lang][key]) ?? I18N.zh[key] ?? key;
    if (typeof s === "function") return s(...args);
    return s;
  }

  // Translate known backend (Flask) error messages.
  const ERROR_MAP = [
    [/^未登录$/, "errNotLoggedIn"],
    [/^配置文件 (.+) 不存在$/, "errConfigMissing"],
    [/^name 和 content 不能为空$/, "errNameContentRequired"],
    [/^文件名不合法$/, "errInvalidFileName"],
    [/^文件 (.+) 已存在$/, "errFileExists"],
    [/^name 不能为空$/, "errNameRequired"],
    [/^content 不能为空$/, "errContentRequired"],
    [/^切换失败: (.*)$/, "errSwitchFailed"],
  ];
  function translateError(msg) {
    if (!msg) return "";
    for (const [re, key] of ERROR_MAP) {
      const m = msg.match(re);
      if (m) return t(key, ...m.slice(1));
    }
    return msg;
  }

  function applyStatic() {
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    document.documentElement.lang = getLang() === "zh" ? "zh-CN" : "en";
    document.title = t("title");
    const btn = document.getElementById("langToggle");
    if (btn) btn.textContent = getLang() === "zh" ? "EN" : "中文";
    // server-rendered error (login page)
    const loginErr = document.getElementById("loginError");
    if (loginErr && loginErr.textContent.trim()) {
      loginErr.textContent = translateError(loginErr.textContent.trim());
    }
  }

  function toggleLang() {
    setLang(getLang() === "zh" ? "en" : "zh");
    applyStatic();
    if (typeof window.onLangChange === "function") window.onLangChange();
  }

  window.t = t;
  window.translateError = translateError;
  window.I18N = { toggleLang, getLang, applyStatic };

  document.addEventListener("DOMContentLoaded", applyStatic);
})();
