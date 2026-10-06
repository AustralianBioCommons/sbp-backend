// Replaces each JSONTreeField's raw-text div with a read-only jsoneditor
// widget (view/code/text mode switcher built in).
$(function () {
  // Masked rather than removed, so the key is still visible as redacted.
  var MASKED_KEYS = ["computeEnvId", "workspaceId"];

  $("div.field-json").each(function () {
    var el = this;
    var data;
    try {
      data = JSON.parse($(el).text());
    } catch (e) {
      return;
    }
    if (data && typeof data === "object" && !Array.isArray(data)) {
      MASKED_KEYS.forEach(function (key) {
        if (key in data) data[key] = "***";
      });
    }
    $(el).empty();
    new JSONEditor(el, { mode: "view", modes: ["view", "code", "text"] }, data);
  });
});
