function doPost(e) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    
    // Автоматическое создание красивой шапки, если таблица пустая
    if (sheet.getLastRow() === 0) {
      sheet.appendRow([
        "ID", "Дата создания", "Статус", "Запланирован на",
        "Заголовок / Тема", "Источник", "Просмотры",
        "Telegram пост", "Instagram карусель", "Threads тред"
      ]);
      sheet.getRange(1, 1, 1, 10).setFontWeight("bold").setBackground("#e8f0fe");
      sheet.setFrozenRows(1);
    }

    var data = JSON.parse(e.postData.contents);
    
    // Поиск существующей строки по ID для обновления, чтобы не плодить дубли
    var dataRange = sheet.getDataRange().getValues();
    var existingRow = -1;
    for (var i = 1; i < dataRange.length; i++) {
      if (dataRange[i][0] == data.id) {
        existingRow = i + 1;
        break;
      }
    }

    var statusRu = data.status || "pending";
    if (statusRu === "published") statusRu = "Опубликован";
    else if (statusRu === "scheduled") statusRu = "Запланирован";
    else if (statusRu === "pending") statusRu = "Черновик";

    var rowValues = [
      data.id || "",
      data.created_at || new Date().toISOString().substring(0, 16),
      statusRu,
      data.scheduled_at || "—",
      data.title || "",
      data.source_url || "—",
      data.views_count || 0,
      data.tg_post || "",
      data.ig_post || "",
      data.threads_post || ""
    ];

    if (existingRow > 0) {
      sheet.getRange(existingRow, 1, 1, rowValues.length).setValues([rowValues]);
    } else {
      sheet.appendRow(rowValues);
    }

    return ContentService.createTextOutput(JSON.stringify({ status: "success" }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({ status: "error", message: error.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
