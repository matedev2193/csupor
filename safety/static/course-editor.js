'use strict';
(() => {
  const form = document.getElementById('course-form');
  if (!form) return;
  const list = document.getElementById('questions');
  const builder = document.getElementById('exam-builder');
  const output = document.getElementById('questions-json');
  let nextId = 0;
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }
  function renumber() {
    [...list.children].forEach((card, index) => { card.querySelector('.question-number').textContent = `${index + 1}. kérdés`; });
    document.getElementById('empty-questions').hidden = list.children.length > 0;
    list.querySelectorAll('.question-card').forEach(card => {
      const rows = card.querySelectorAll('.option-row');
      rows.forEach(row => { row.querySelector('.remove-option').disabled = rows.length <= 2; });
      card.querySelector('.add-option').disabled = rows.length >= 6;
    });
  }
  function addOption(card, text = '', correct = false) {
    const row = element('div', 'option-row');
    const label = element('label', 'correct-choice');
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = `correct_${card.dataset.key}`; radio.checked = correct; radio.setAttribute('aria-label', 'Ez a helyes válasz');
    label.append(radio, document.createTextNode('Helyes'));
    const input = document.createElement('input');
    input.type = 'text'; input.className = 'option-text'; input.value = text; input.maxLength = 500; input.placeholder = 'Válaszlehetőség'; input.setAttribute('aria-label', 'Válaszlehetőség'); input.required = true;
    const remove = element('button', 'button button-quiet button-small remove-option', '×');
    remove.type = 'button'; remove.setAttribute('aria-label', 'Válaszlehetőség törlése');
    remove.addEventListener('click', () => {
      if (card.querySelectorAll('.option-row').length <= 2) return;
      const wasCorrect = radio.checked; row.remove();
      if (wasCorrect) card.querySelector('.correct-choice input').checked = true;
      renumber();
    });
    row.append(label, input, remove); card.querySelector('.options-container').append(row);
    renumber();
  }
  function addQuestion(question = {prompt: '', options: ['', ''], correct_index: 0}) {
    if (list.children.length >= 100) return;
    const card = element('fieldset', 'question-card'); card.dataset.key = String(nextId++);
    const header = element('div', 'question-head');
    const number = element('span', 'question-number');
    const remove = element('button', 'button button-danger button-small', 'Kérdés törlése'); remove.type = 'button';
    remove.addEventListener('click', () => { card.remove(); renumber(); });
    header.append(number, remove);
    const label = element('label', '', 'Kérdés szövege');
    const prompt = document.createElement('textarea'); prompt.className = 'question-prompt'; prompt.value = question.prompt || ''; prompt.maxLength = 2000; prompt.required = true; prompt.rows = 2;
    label.append(prompt);
    const choices = element('div', 'options-container');
    const add = element('button', 'button button-quiet button-small add-option', '+ Válaszlehetőség'); add.type = 'button';
    add.addEventListener('click', () => { if (card.querySelectorAll('.option-row').length < 6) addOption(card); });
    card.append(header, label, choices, add); list.append(card);
    (Array.isArray(question.options) ? question.options : ['', '']).forEach((option, index) => addOption(card, option, index === Number(question.correct_index || 0)));
    renumber(); updateMode();
  }
  function updateMode() {
    const exam = form.querySelector('input[name="mode"]:checked').value === 'exam';
    builder.hidden = !exam;
    document.getElementById('passing-percent').disabled = !exam;
    list.querySelectorAll('fieldset').forEach(fieldset => { fieldset.disabled = !exam; });
  }
  let initial = [];
  try { initial = JSON.parse(form.dataset.questions || '[]'); } catch { initial = []; }
  if (Array.isArray(initial)) initial.forEach(addQuestion);
  document.getElementById('add-question').addEventListener('click', () => addQuestion());
  form.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', updateMode));
  form.addEventListener('submit', event => {
    let questions = [...list.querySelectorAll('.question-card')].map(card => {
      const rows = [...card.querySelectorAll('.option-row')];
      return {prompt: card.querySelector('.question-prompt').value.trim(), options: rows.map(row => row.querySelector('.option-text').value.trim()), correct_index: rows.findIndex(row => row.querySelector('input[type="radio"]').checked)};
    });
    const error = document.getElementById('builder-error');
    const isExam = form.querySelector('input[name="mode"]:checked').value === 'exam';
    if (!isExam) questions = questions.filter(q => q.prompt && q.options.every(Boolean) && q.correct_index >= 0 && new Set(q.options).size === q.options.length);
    if (questions.some(q => !q.prompt || q.options.some(option => !option) || q.correct_index < 0 || new Set(q.options).size !== q.options.length)) {
      event.preventDefault(); error.textContent = 'Töltsd ki a kérdések és válaszok szövegét, és minden kérdésnél jelöld meg a helyes választ. A válaszlehetőségek legyenek különbözőek.'; error.hidden = false;
      if (builder.hidden) { form.querySelector('input[name="mode"][value="exam"]').checked = true; updateMode(); }
      return;
    }
    output.value = JSON.stringify(questions); error.hidden = true;
  });
  renumber(); updateMode();
})();
