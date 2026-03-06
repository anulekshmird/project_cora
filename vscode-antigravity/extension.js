
const vscode = require('vscode');
const http = require('http');

let debounceTimer;

function activate(context) {
    console.log('Antigravity VS Code Bridge is active!');

    // 1. Listen for text changes
    vscode.workspace.onDidChangeTextDocument(event => {
        const document = event.document;

        // Filter relevant files (Python only for now)
        if (document.languageId !== 'python') return;

        // Debounce (Wait 500ms after last keystroke)
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            pushContent(document);
        }, 500);
    });

    // 2. Listen for active editor changes
    vscode.window.onDidChangeActiveTextEditor(editor => {
        if (editor && editor.document.languageId === 'python') {
            pushContent(editor.document);
        }
    });
}

function pushContent(document) {
    const content = document.getText();
    const filePath = document.fileName;

    const data = JSON.stringify({
        file_path: filePath,
        buffer_content: content,
        language: document.languageId
    });

    const options = {
        hostname: '127.0.0.1',
        port: 54321,
        path: '/update_buffer',
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(data)
        }
    };

    const req = http.request(options, (res) => {
        // console.log(`STATUS: ${res.statusCode}`);
    });

    req.on('error', (e) => {
        console.error(`Problem with request: ${e.message}`);
    });

    req.write(data);
    req.end();
}

function deactivate() { }

module.exports = {
    activate,
    deactivate
};
