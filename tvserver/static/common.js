// Společné horní menu: zvýraznění aktuální stránky.
// window.lanOrigin: adresa serveru v domácí síti (stránka může být otevřená přes 127.0.0.1,
// a ta adresa na Apple TV ani na telefonu nefunguje).
window.lanOrigin = fetch('/api/info').then(r => r.json()).then(i => i.ip_url || location.origin).catch(() => location.origin);
document.addEventListener('DOMContentLoaded', () => {
  const path = location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.top .nav a').forEach(a => {
    const href = a.getAttribute('href');
    if (href === path || (href !== '/' && path.startsWith(href + '/'))) a.setAttribute('aria-current', 'page');
  });
});
