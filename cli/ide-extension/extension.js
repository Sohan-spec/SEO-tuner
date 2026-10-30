// The seofixer CLI opens <editor>://seofixer.seofixer-preview/open?url=... and we show that page in the built-in browser.
const vscode = require('vscode');

exports.activate = (context) => {
  context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri) {
      const url = new URLSearchParams(uri.query).get('url') || '';
      if (/^http:\/\/127\.0\.0\.1:\d+\//.test(url)) { // only the CLI's own local preview
        vscode.commands.executeCommand('simpleBrowser.api.open', vscode.Uri.parse(url), { preserveFocus: true });
      }
    },
  }));
};
