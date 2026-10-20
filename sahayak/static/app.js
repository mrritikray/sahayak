'use strict';
const toggle = document.querySelector('.menu-toggle');
const nav = document.querySelector('.sidebar');
if (toggle && nav) {
  toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  });
  const closeNav = () => { nav.classList.remove('open'); toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); };
  document.querySelector('.nav-close').addEventListener('click', closeNav);
  document.addEventListener('click', event => {
    if (window.innerWidth <= 640 && nav.classList.contains('open') && !nav.contains(event.target) && !toggle.contains(event.target)) closeNav();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && nav.classList.contains('open')) closeNav();
  });
}
const dialog = document.getElementById('confirm-dialog');
let pending = null;
document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.dataset.approved === 'yes') {
      delete form.dataset.approved;
      return;
    }
    event.preventDefault();
    if (form.classList.contains('confirm-dose-form')) {
      const choice = form.querySelector('select');
      if (!choice || !choice.value) return;
    }
    pending = { form, submitter: event.submitter };
    document.getElementById('confirm-text').textContent = form.dataset.confirm;
    dialog.showModal();
  });
});
document.getElementById('confirm-cancel').addEventListener('click', () => { pending = null; dialog.close(); });
dialog.addEventListener('cancel', () => { pending = null; });
document.getElementById('confirm-yes').addEventListener('click', () => {
  if (!pending) return;
  const { form, submitter } = pending;
  pending = null;
  form.dataset.approved = 'yes';
  dialog.close();
  form.requestSubmit(submitter || undefined);
});
