// Tiny JSON helper for produce.bat. No extra runtime — Windows cscript only.
// Usage:
//   cscript //nologo produce_json.js get    <file> <key>
//   cscript //nologo produce_json.js names  <file>
//   cscript //nologo produce_json.js hmes   <file>
//   cscript //nologo produce_json.js retry  <text>

var mode = WScript.Arguments.length ? String(WScript.Arguments(0)) : "";
var arg1 = WScript.Arguments.length > 1 ? String(WScript.Arguments(1)) : "";
var arg2 = WScript.Arguments.length > 2 ? String(WScript.Arguments(2)) : "";

function readFile(path) {
  var fso = WScript.CreateObject("Scripting.FileSystemObject");
  var fh = fso.OpenTextFile(path, 1);
  var text = fh.ReadAll();
  fh.Close();
  return text;
}

function firstMatch(text, re) {
  var m = text.match(re);
  return m ? m[1] : "";
}

function allMatches(text, re) {
  var out = [];
  var m;
  re.lastIndex = 0;
  while ((m = re.exec(text))) out.push(m[1]);
  return out;
}

if (mode === "get") {
  var text = readFile(arg1);
  var key = arg2.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  var value = firstMatch(text, new RegExp('"' + key + '"\\s*:\\s*"([^"]*)"'));
  if (value === "") value = firstMatch(text, new RegExp('"' + key + '"\\s*:\\s*(-?\\d+)'));
  if (value !== "") WScript.Echo(value);
} else if (mode === "names") {
  emitReadyNames(readFile(arg1));
} else if (mode === "hmes") {
  var hmes = allMatches(readFile(arg1), /"hme"\s*:\s*"([^"]+)"/g);
  for (var j = 0; j < hmes.length; j++) WScript.Echo(hmes[j]);
} else if (mode === "retry" || mode === "retryfile") {
  var src = mode === "retryfile" ? readFile(arg1) : String(arg1);
  var hit = src.match(/retry_after\s*=\s*(\d+)/i);
  WScript.Echo(hit ? hit[1] : "30");
} else {
  WScript.Echo("usage: produce_json.js get|names|hmes|retry ...");
  WScript.Quit(2);
}

function emitReadyNames(text) {
  var start = text.indexOf('"accounts"');
  var chunk = start >= 0 ? text.slice(start) : text;
  var items = chunk.split(/\{/);
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var name = firstMatch(item, /"name"\s*:\s*"([^"]+)"/);
    if (!name) continue;
    if (/"cookie_invalid"\s*:\s*true/.test(item)) continue;
    if (/"hme_ok"\s*:\s*false/.test(item)) continue;
    WScript.Echo(name);
  }
}
