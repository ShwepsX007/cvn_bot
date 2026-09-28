/*
 * Информационный баннер о cookies (технические cookies: сессия входа).
 * Показывается один раз; согласие запоминается в localStorage браузера.
 */
(function () {
    function show() {
        try {
            if (localStorage.getItem('awg_cookie_consent') === '1') return;
        } catch (e) { /* приватный режим - показываем баннер каждый раз, ничего страшного */ }

        if (document.getElementById('cookie-consent-bar')) return;

        var bar = document.createElement('div');
        bar.id = 'cookie-consent-bar';
        bar.style.cssText =
            'position:fixed;left:0;right:0;bottom:0;z-index:1060;' +
            'background:#161b22;color:#e6edf3;border-top:1px solid #30363d;' +
            'padding:14px 18px;display:flex;gap:14px;align-items:center;justify-content:center;' +
            'flex-wrap:wrap;font-size:14px;box-shadow:0 -6px 24px rgba(0,0,0,.35);';
        bar.innerHTML =
            '<span>🍪 Мы используем технические cookies для работы входа в личный кабинет. ' +
            'Подробнее: <a href="/terms" style="color:#58a6ff">Пользовательское соглашение</a> · ' +
            '<a href="/privacy" style="color:#58a6ff">Политика конфиденциальности</a>.</span>' +
            '<button type="button" id="cookie-consent-ok" style="' +
            'background:#238636;color:#fff;border:none;border-radius:999px;padding:8px 22px;font-weight:600;cursor:pointer;' +
            '">Понятно</button>';

        document.body.appendChild(bar);
        document.getElementById('cookie-consent-ok').addEventListener('click', function () {
            try { localStorage.setItem('awg_cookie_consent', '1'); } catch (e) {}
            bar.remove();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', show);
    } else {
        show();
    }
})();
