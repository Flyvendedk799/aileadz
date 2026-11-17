/* Actor-scoped action recovery on every authenticated surface. */
(function () {
  'use strict';
  var button = document.createElement('button');
  button.type = 'button'; button.textContent = 'AI';
  button.className = 'fm-icon-btn'; button.title = 'AI-handlinger';
  button.setAttribute('aria-label', 'AI-handlinger');
  button.setAttribute('aria-haspopup', 'dialog');
  var dialog = document.createElement('dialog');
  dialog.className = 'fm-ai-inbox';
  dialog.setAttribute('aria-label', 'Dine AI-handlinger');
  var close = document.createElement('button'); close.textContent = 'Luk'; close.type = 'button';
  close.onclick = function () { dialog.close(); };
  var content = document.createElement('div');
  content.setAttribute('aria-live', 'polite');
  dialog.append(close, content); document.body.appendChild(dialog);
  (document.querySelector('.fm-top-actions, .vp-top') || document.body).appendChild(button);
  var labels = {pending:'Afventer bekræftelse', running:'Behandles', consumed:'Gennemført', complete:'Svar færdigt',
    partial:'Delvist gennemført – kræver opfølgning', uncertain:'Usikkert resultat – kontroller før ny handling',
    rejected:'Afvist', failed:'Fejlet', expired:'Udløbet', cancelled:'Stoppet'};
  button.onclick = async function () {
    dialog.showModal(); content.textContent = 'Henter handlinger…';
    try {
      var response = await fetch('/app1/actions', {credentials:'same-origin'});
      if (!response.ok) throw new Error();
      var data = await response.json(); content.textContent = '';
      if (!data.actions.length) content.textContent = 'Du har ingen gemte AI-handlinger.';
      data.actions.forEach(function (item) {
        var section = document.createElement('section'), title = document.createElement('h3');
        title.textContent = item.card.summary_da || item.name; section.appendChild(title);
        var status = document.createElement('p'); status.textContent = labels[item.status] || item.status; section.appendChild(status);
        if (item.kind === 'confirmation' && item.status === 'pending') {
          FMAI.confirmCard(section, Object.assign({}, item.card, {token:item.id}), {
            confirmUrl:item.status_url + '/confirm', rejectUrl:item.status_url + '/reject'});
        }
        if (item.receipt.message || item.receipt.message_da || item.receipt.confirmation_text) {
          var receipt = document.createElement('p'); receipt.textContent = item.receipt.message_da || item.receipt.message || item.receipt.confirmation_text;
          section.appendChild(receipt);
        }
        if (item.kind === 'artifact' && item.status === 'complete') {
          var download = document.createElement('a'); download.href = item.status_url + '/download'; download.textContent = 'Hent fil'; section.appendChild(download);
        }
        if (item.kind === 'turn' && item.status === 'running') {
          var cancel = document.createElement('button'); cancel.textContent = 'Stop';
          cancel.onclick = async function () { await fetch(item.status_url + '/cancel', {method:'POST'}); button.onclick(); };
          section.appendChild(cancel);
        }
        content.appendChild(section);
      });
    } catch (e) { content.textContent = 'Handlingerne kunne ikke hentes. Prøv igen senere.'; }
  };
  window.fmOpenAiActions = function () { return button.onclick(); };
}());
