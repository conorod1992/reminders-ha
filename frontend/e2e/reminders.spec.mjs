import { test, expect } from "@playwright/test";

// This suite uses a *real* authenticated Home Assistant instance. It is not
// run in PR CI against a disposable installation, or manually with an existing
// authorized test session.
const configured = Boolean(process.env.HA_URL && process.env.HA_STORAGE_STATE);
test.skip(!configured, "Set HA_URL and HA_STORAGE_STATE (see README) first.");

async function openReminders(page) {
  await page.goto("/reminders");
  const panel = page.locator("reminders-management-panel");
  await expect(panel).toBeVisible();
  await expect(panel.locator("h1")).toHaveText("Reminders");
  await expect(panel.locator("#list .loading")).toHaveCount(0);
  return panel;
}

test("authenticated panel loads, switches views and keeps the search usable", async ({ page }) => {
  const panel = await openReminders(page);
  await panel.getByRole("tab", { name: "All reminders" }).click();
  await expect(panel.getByRole("tab", { name: "All reminders" })).toHaveAttribute("aria-selected", "true");
  await panel.getByRole("tab", { name: "History" }).click();
  await expect(panel.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
  const search = panel.locator('input[type="search"]');
  await search.fill("test-not-present-" + Date.now());
  await expect(search).toHaveValue(/test-not-present-/);
  await panel.getByRole("tab", { name: "Upcoming" }).click();
  await expect(panel.locator("#error")).toBeEmpty();
});

test("form cancellation protects unsaved work and restores focus", async ({ page }) => {
  const panel = await openReminders(page);
  const add = panel.getByRole("button", { name: /add reminder/i });
  await add.click();
  const dialog = panel.locator("dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator('[name="title"]').fill("PR4 unsaved test");
  page.once("dialog", async (confirm) => {
    expect(confirm.type()).toBe("confirm");
    await confirm.dismiss();
  });
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeVisible();
  page.once("dialog", async (confirm) => confirm.accept());
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(add).toBeFocused();
});

test("notification deep-link rejects non-existent reminder without leaking a card", async ({ page }) => {
  await page.goto("/reminders?reminder_id=pr4-nonexistent-identifier");
  const panel = page.locator("reminders-management-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Opened from notification")).toHaveCount(0);
});

// Mutation tests require explicit opt-in. They create and then delete an
// identifiable disposable reminder; never enable against production by default.
test("create, find and delete a disposable reminder through the real UI", async ({ page }) => {
  test.skip(process.env.HA_E2E_MUTATE !== "1", "Mutation tests require HA_E2E_MUTATE=1.");
  const panel = await openReminders(page);
  const title = "PR4 E2E " + Date.now();
  await panel.getByRole("button", { name: /add reminder/i }).click();
  const dialog = panel.locator("dialog");
  await dialog.locator('[name="title"]').fill(title);
  // Use the visible Tomorrow shortcut rather than browser-local date values.
  await dialog.locator('[data-quick="tomorrow"]').click();
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toBeVisible();
  await panel.getByRole("tab", { name: "All reminders" }).click();
  await panel.locator('input[type="search"]').fill(title);
  const card = panel.locator(".card", { hasText: title });
  await expect(card).toBeVisible();
  await card.locator("summary[aria-label='More actions']").click();
  await card.getByRole("button", { name: "Delete" }).click();
  const confirm = panel.locator("dialog");
  await confirm.getByRole("button", { name: "Delete" }).click();
  await expect(card).toHaveCount(0);
});

test("non-admin can open the editor without administrator-only mobile services", async ({ browser }) => {
  test.skip(!process.env.HA_NONADMIN_STORAGE_STATE,
    "Provide HA_NONADMIN_STORAGE_STATE to test actual non-admin permissions.");
  const context = await browser.newContext({
    storageState: process.env.HA_NONADMIN_STORAGE_STATE,
  });
  try {
    const page = await context.newPage();
    const panel = await openReminders(page);
    await panel.getByRole("button", { name: /add reminder/i }).click();
    const dialog = panel.locator("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".mobile-app-label")).toBeHidden();
    await dialog.getByRole("button", { name: "Cancel" }).click();
  } finally {
    await context.close();
  }
});

test("non-admin cannot view a different user's reminder via deep link", async ({ browser }) => {
  test.skip(!process.env.HA_NONADMIN_STORAGE_STATE || !process.env.HA_OTHER_USER_REMINDER_ID,
    "Provide separate-account state and a disposable other-user reminder ID.");
  const context = await browser.newContext({
    storageState: process.env.HA_NONADMIN_STORAGE_STATE,
  });
  try {
    const page = await context.newPage();
    await page.goto("/reminders?reminder_id=" +
      encodeURIComponent(process.env.HA_OTHER_USER_REMINDER_ID));
    const panel = page.locator("reminders-management-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("Opened from notification")).toHaveCount(0);
  } finally {
    await context.close();
  }
});


test("keyboard tab focus survives an asynchronous data refresh", async ({ page }) => {
  const panel = await openReminders(page);
  const upcoming = panel.getByRole("tab", { name: "Upcoming" });
  await upcoming.focus();
  await upcoming.press("ArrowRight");
  await expect(panel.getByRole("tab", { name: "All reminders" })).toBeFocused();
  await panel.evaluate(async (element) => { await element._load(); });
  await expect(panel.getByRole("tab", { name: "All reminders" })).toBeFocused();
});

test("renaming a classic reminder preserves its delivery condition", async ({page}) => {
  test.skip(process.env.HA_E2E_MUTATE !== "1", "Requires disposable test mutations");
  const panel = await openReminders(page);
  const title = "Classic editor regression " + Date.now();
  const reminder = await panel.evaluate(async (element, title) => {
    const result = await element._hass.callWS({type:"reminders/create", title, due:new Date(Date.now()+86400000).toISOString(), deliver_when:{type:"state",entity_id:"input_boolean.review_ready",to:"on"}});
    return result.reminder;
  }, title);
  try {
    await panel.evaluate(async (element) => { await element._load(); });
    await panel.evaluate((element, reminder) => element._openReminderForm(reminder), reminder);
    const dialog = panel.locator("dialog");
    await dialog.locator('[name="title"]').fill(title + " renamed");
    await dialog.locator('button[type="submit"]').click();
    await expect(dialog).not.toBeVisible();
    const saved = await panel.evaluate(async (element,id) => (await element._hass.callWS({type:"reminders/get",reminder_id:id})).reminder,reminder.id);
    expect(saved.deliver_when).toEqual(reminder.deliver_when);
    expect(saved.delivery_triggers).toEqual([]);
  } finally {
    await panel.evaluate(async (element,id) => element._hass.callWS({type:"reminders/delete",reminder_id:id}),reminder.id);
  }
});
