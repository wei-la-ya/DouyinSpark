"""账号配置页 HTML（移植自 Yunzai 版 web-setup.js 的 renderSetupPage）

服务端占位符：__TITLE__ __TOKEN__ __DATA__ __PREFIX__ __COOKIE_REQUIRED__ __COOKIE_PLACEHOLDER__ __SUBMIT_LABEL__
"""

_SETUP_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="data:,">
  <title>__TITLE__</title>
  <style>
    :root { color-scheme: light; font-family: "Microsoft YaHei", sans-serif; color: #1d2939; background: #f4f7fb; }
    body { margin: 0; padding: 32px 16px; }
    main { width: min(680px, 100%); margin: 0 auto; background: #fff; border: 1px solid #d7dee8; border-radius: 8px; box-shadow: 0 10px 30px #17203314; overflow: hidden; }
    header { padding: 24px 28px; background: #173b60; color: #fff; }
    h1 { margin: 0; font-size: 22px; font-weight: 600; }
    form { padding: 28px; display: grid; gap: 18px; }
    label { display: grid; gap: 8px; font-size: 14px; font-weight: 600; }
    input, textarea { box-sizing: border-box; width: 100%; border: 1px solid #b9c5d3; border-radius: 5px; padding: 10px 12px; font: inherit; color: #172033; background: #fff; }
    textarea { min-height: 88px; resize: vertical; line-height: 1.5; }
    input:focus, textarea:focus { outline: 2px solid #4b9edb66; border-color: #247bb7; }
    .hint { margin: 0; color: #667085; font-size: 12px; font-weight: 400; line-height: 1.5; }
    .cookie { min-height: 160px; font-family: Consolas, monospace; font-size: 12px; }
    .check { display: flex; align-items: center; gap: 8px; font-weight: 400; }
    .check input { width: 16px; height: 16px; }
    button { justify-self: start; border: 0; border-radius: 5px; padding: 11px 20px; background: #1976b7; color: #fff; font: inherit; cursor: pointer; }
    button:disabled { cursor: wait; opacity: .65; }
    .scan { display: grid; gap: 8px; padding: 12px; border: 1px solid #d7dee8; border-radius: 5px; background: #f8fafc; }
    .targets { display: grid; gap: 8px; padding: 12px; border: 1px solid #d7dee8; border-radius: 5px; background: #f8fafc; font-size: 14px; }
    .conv-list { display: grid; gap: 4px; max-height: 280px; overflow-y: auto; }
    .conv-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border: 1px solid #e4eaf1; border-radius: 4px; background: #fff; font-weight: 400; }
    .conv-item input { width: 16px; height: 16px; }
    .conv-item .uid { color: #98a2b3; font-size: 11px; margin-left: auto; }
    .conv-item .avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; background: #e4eaf1; flex: none; }
    .scan-actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .scan button { justify-self: start; }
    .qr { display: none; width: min(360px, 100%); max-height: 360px; object-fit: contain; border: 1px solid #d7dee8; background: #fff; }
    .sms { display: none; gap: 8px; grid-template-columns: minmax(0, 1fr) auto; }
    .sms input { min-width: 0; }
    #status { margin: 0; min-height: 20px; color: #b42318; font-size: 14px; }
    #status.ok { color: #087443; }
  </style>
</head>
<body>
  <main>
    <header><h1>__TITLE__</h1></header>
    <form id="setup-form">
      <label>账号名称<input id="name" maxlength="40" required></label>
      <label>消息模板<textarea id="messageTemplate" placeholder="留空使用随机一言"></textarea></label>
      <div class="targets">
        <span class="hint">续火目标：点击下方按钮，通过接口读取私信会话里出现过的人，勾选后保存（按用户 ID 发送，对方改名不影响送达）。</span>
        <div class="scan-actions">
          <button id="loadConvs" type="button">拉取会话列表</button>
        </div>
        <span id="convStatus" class="hint">拉取需要 10 到 60 秒（含昵称查询），请耐心等待。</span>
        <div id="convList" class="conv-list"></div>
      </div>
      <label>失败通知邮箱<input id="email" type="email" placeholder="留空则不发送失败邮件"></label>
      <label class="check"><input id="successEmailEnabled" type="checkbox">续火成功时发送邮件通知</label>
      <label>Cookie 文本文件<input id="cookieFile" type="file" accept=".txt,text/plain"><span class="hint">选择后会读取到下方文本框，不会上传文件本身。</span></label>
      <div class="scan">
        <div class="scan-actions">
          <button id="scanLogin" type="button">扫码获取 Cookie</button>
          <button id="scanRefresh" type="button" disabled>刷新二维码</button>
        </div>
        <img id="scanQr" class="qr" alt="抖音登录二维码">
        <span id="scanStatus" class="hint">也可以直接粘贴 Cookie JSON 或选择 .txt 文件。</span>
        <div id="smsVerify" class="sms">
          <input id="smsCode" inputmode="numeric" autocomplete="one-time-code" placeholder="输入短信验证码">
          <button id="smsSubmit" type="button">提交验证码</button>
        </div>
      </div>
      <div class="scan">
        <span class="hint">手机号登录也限流时，请用真实浏览器登录 douyin.com → Cookie-Editor 导出 Cookie JSON → 粘到下方文本框。</span>
        <div class="scan-actions">
          <input id="smsMobile" inputmode="tel" autocomplete="tel" placeholder="手机号（登录抖音的）" style="max-width:220px">
          <button id="smsSend" type="button">发送验证码</button>
        </div>
        <div id="smsLoginRow" class="sms">
          <input id="smsLoginCode" inputmode="numeric" autocomplete="one-time-code" placeholder="输入短信验证码">
          <button id="smsLoginSubmit" type="button">短信登录</button>
        </div>
        <span id="smsStatus" class="hint"></span>
      </div>
      <label>Cookie JSON<textarea id="cookieText" class="cookie" __COOKIE_REQUIRED__ placeholder="__COOKIE_PLACEHOLDER__"></textarea></label>
      <p id="status"></p>
      <button id="submit" type="submit">__SUBMIT_LABEL__</button>
    </form>
  </main>
  <script>
    const initial = __DATA__;
    const form = document.querySelector('#setup-form');
    const status = document.querySelector('#status');
    const submit = document.querySelector('#submit');
    const scanLogin = document.querySelector('#scanLogin');
    const scanRefresh = document.querySelector('#scanRefresh');
    const scanQr = document.querySelector('#scanQr');
    const scanStatus = document.querySelector('#scanStatus');
    const smsVerify = document.querySelector('#smsVerify');
    const smsCode = document.querySelector('#smsCode');
    const smsSubmit = document.querySelector('#smsSubmit');
    let scanTimer;
    for (const key of ['name', 'messageTemplate', 'email']) document.querySelector('#' + key).value = initial[key] || '';
    document.querySelector('#successEmailEnabled').checked = Boolean(initial.successEmailEnabled);

    // ===== 续火目标选择 =====
    const loadConvs = document.querySelector('#loadConvs');
    const convStatus = document.querySelector('#convStatus');
    const convList = document.querySelector('#convList');
    const knownTargets = new Map();
    function renderTargets() {
      convList.innerHTML = '';
      for (const target of knownTargets.values()) {
        const item = document.createElement('label');
        item.className = 'conv-item';
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = target.checked;
        box.addEventListener('change', () => { target.checked = box.checked; });
        if (target.avatar) {
          const img = document.createElement('img');
          img.className = 'avatar';
          img.src = target.avatar;
          img.alt = '';
          img.loading = 'lazy';
          img.addEventListener('error', () => { img.style.display = 'none' });
          item.append(img);
        }
        const name = document.createElement('span');
        name.textContent = target.nickname || '（昵称未获取）';
        item.append(box, name);
        if (target.uniqueId) {
          const uid = document.createElement('span');
          uid.className = 'uid';
          uid.textContent = '抖音号: ' + target.uniqueId;
          item.append(uid);
        }
        convList.append(item);
      }
      const total = knownTargets.size;
      const chosen = [...knownTargets.values()].filter(item => item.checked).length;
      if (total > 0) convStatus.textContent = '共 ' + total + ' 人，已勾选 ' + chosen + ' 人；保存后生效。';
    }
    for (const target of initial.targets || []) {
      knownTargets.set(target.secUid, { ...target, checked: true });
    }
    renderTargets();
    loadConvs.addEventListener('click', async () => {
      loadConvs.disabled = true;
      convStatus.textContent = '正在通过接口拉取会话列表，可能需要 10 到 60 秒…';
      try {
        const response = await fetch('__PREFIX__/api/conversations/__TOKEN__', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cookieText: document.querySelector('#cookieText').value }),
        });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '拉取会话列表失败');
        let added = 0;
        for (const person of data.list || []) {
          if (!knownTargets.has(person.secUid)) {
            knownTargets.set(person.secUid, { ...person, checked: false });
            added += 1;
          }
        }
        renderTargets();
        convStatus.textContent = '拉取完成：会话共 ' + ((data.list || []).length) + ' 人，新增 ' + added + ' 人待勾选（已勾选的保持不变）。';
      } catch (error) {
        convStatus.textContent = error.message || '拉取会话列表失败';
      } finally {
        loadConvs.disabled = false;
      }
    });

    document.querySelector('#cookieFile').addEventListener('change', async event => {
      const file = event.target.files[0];
      if (!file) return;
      if (!/\\.txt$/i.test(file.name)) { status.textContent = '仅支持 .txt 文件。'; return; }
      if (file.size > 1024 * 1024) { status.textContent = '文件不能超过 1 MB。'; return; }
      document.querySelector('#cookieText').value = await file.text();
      status.textContent = '';
    });
    async function requestQr(endpoint, loadingText) {
      scanLogin.disabled = true;
      scanRefresh.disabled = true;
      smsVerify.style.display = 'none';
      smsCode.value = '';
      scanStatus.textContent = loadingText;
      try {
        const response = await fetch(endpoint, { method: 'POST' });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '启动扫码登录失败');
        if (data.qr) { scanQr.src = data.qr; scanQr.style.display = 'block'; }
        scanStatus.textContent = '请使用抖音 App 扫码登录，二维码有效期以页面为准。';
        scanRefresh.disabled = false;
        clearInterval(scanTimer);
        scanTimer = setInterval(async () => {
          try {
            const result = await (await fetch('__PREFIX__/api/scan/status/__TOKEN__', { cache: 'no-store' })).json();
            if (!result.ok) throw new Error(result.message || '读取扫码状态失败');
            if (result.status === 'success') {
              clearInterval(scanTimer);
              smsVerify.style.display = 'none';
              document.querySelector('#cookieText').value = JSON.stringify(result.cookies, null, 2);
              scanStatus.textContent = '扫码登录成功，Cookie 已填入下方文本框，请继续提交。';
              scanLogin.disabled = false;
              scanRefresh.disabled = false;
              scanLogin.textContent = '重新扫码';
            } else if (result.status === 'sms') {
              smsVerify.style.display = 'grid';
              scanStatus.textContent = result.message || '请输入短信验证码。';
            } else if (result.status === 'error') {
              throw new Error(result.message || '扫码登录失败');
            } else if (result.message) {
              scanStatus.textContent = result.message;
            }
          } catch (error) {
            clearInterval(scanTimer);
            scanStatus.textContent = error.message || '读取扫码状态失败';
            scanLogin.disabled = false;
            scanRefresh.disabled = false;
          }
        }, 2000);
      } catch (error) {
        scanStatus.textContent = error.message || '启动扫码登录失败';
        scanLogin.disabled = false;
        scanRefresh.disabled = false;
      }
    }
    scanLogin.addEventListener('click', () => requestQr('__PREFIX__/api/scan/start/__TOKEN__', '正在获取登录二维码...'));
    scanRefresh.addEventListener('click', () => requestQr('__PREFIX__/api/scan/refresh/__TOKEN__', '正在刷新二维码...'));
    smsSubmit.addEventListener('click', async () => {
      const code = smsCode.value.trim();
      if (!/^\\d{4,8}$/.test(code)) { scanStatus.textContent = '请输入 4 到 8 位短信验证码。'; return; }
      smsSubmit.disabled = true;
      try {
        const response = await fetch('__PREFIX__/api/scan/sms/__TOKEN__', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '提交短信验证码失败');
        scanStatus.textContent = data.message || '验证码已提交，请等待登录结果。';
      } catch (error) {
        scanStatus.textContent = error.message || '提交短信验证码失败';
      } finally {
        smsSubmit.disabled = false;
      }
    });
    // ===== 手机号短信验证码登录 =====
    const smsMobile = document.querySelector('#smsMobile');
    const smsSend = document.querySelector('#smsSend');
    const smsLoginRow = document.querySelector('#smsLoginRow');
    const smsLoginCode = document.querySelector('#smsLoginCode');
    const smsLoginSubmit = document.querySelector('#smsLoginSubmit');
    const smsStatus = document.querySelector('#smsStatus');
    smsSend.addEventListener('click', async () => {
      const mobile = smsMobile.value.trim();
      if (!/^\+?[\d\s-]{6,20}$/.test(mobile)) { smsStatus.textContent = '请输入正确的手机号'; return; }
      smsSend.disabled = true;
      smsStatus.textContent = '正在发送验证码…';
      try {
        const response = await fetch('__PREFIX__/api/sms/send/__TOKEN__', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mobile }) });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '发送失败');
        smsStatus.textContent = data.message || '验证码已发送';
        smsLoginRow.style.display = 'grid';
      } catch (error) {
        smsStatus.textContent = error.message || '发送失败';
      } finally {
        smsSend.disabled = false;
      }
    });
    smsLoginSubmit.addEventListener('click', async () => {
      const code = smsLoginCode.value.trim();
      if (!/^\d{4,8}$/.test(code)) { smsStatus.textContent = '请输入 4 到 8 位验证码'; return; }
      smsLoginSubmit.disabled = true;
      try {
        const response = await fetch('__PREFIX__/api/sms/submit/__TOKEN__', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '登录失败');
        if (data.cookies) document.querySelector('#cookieText').value = JSON.stringify(data.cookies, null, 2);
        smsStatus.textContent = data.message || '登录成功';
      } catch (error) {
        smsStatus.textContent = error.message || '登录失败';
      } finally {
        smsLoginSubmit.disabled = false;
      }
    });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      clearInterval(scanTimer);
      status.className = ''; status.textContent = ''; submit.disabled = true;
      const payload = Object.fromEntries(new FormData(form));
      payload.name = document.querySelector('#name').value;
      payload.messageTemplate = document.querySelector('#messageTemplate').value;
      payload.targets = [...knownTargets.values()].filter(item => item.checked).map(({ checked, ...target }) => target);
      payload.email = document.querySelector('#email').value;
      payload.successEmailEnabled = document.querySelector('#successEmailEnabled').checked;
      payload.cookieText = document.querySelector('#cookieText').value;
      try {
        const response = await fetch('__PREFIX__/api/setup/__TOKEN__', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await response.json();
        if (!data.ok) throw new Error(data.message || '提交失败');
        status.className = 'ok'; status.textContent = data.message; form.querySelectorAll('input, textarea, button').forEach(item => item.disabled = true);
      } catch (error) {
        status.textContent = error.message || '提交失败，请重试。'; submit.disabled = false;
      }
    });
  </script>
</body>
</html>"""
