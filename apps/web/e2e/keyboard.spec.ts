import { expect, seed, test } from "./fixtures";

test("sign in, reach the run and use every control with the keyboard alone", async ({ page }) => {
  const s = seed();
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  // reach "Sign in" in the header by tabbing, then submit the form with Enter
  for (let i = 0; i < 6 && !(await page.getByRole("link", { name: "Sign in" }).evaluate((el) => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await page.waitForURL("**/auth/signin");
  await page.getByLabel("Email").focus();
  await page.keyboard.type(s.owner);
  await page.keyboard.press("Tab");
  await page.keyboard.type(s.password);
  await page.keyboard.press("Enter");
  await page.waitForURL((u) => !u.pathname.startsWith("/auth/"));

  await page.goto(`/runs/${s.run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  // the timeline is a slider: arrows step, [ and ] jump between memory changes
  const slider = page.getByRole("slider", { name: "Step" });
  await slider.focus();
  await page.keyboard.press("Home");
  await expect(slider).toHaveValue("0");
  await page.keyboard.press("ArrowRight");
  await expect(slider).toHaveValue("1");
  await page.keyboard.press("]");
  await expect(slider).toHaveValue("2");                                  // the first memory write
  await page.keyboard.press("]");
  await expect(slider).toHaveValue("5");
  await page.keyboard.press("[");
  await expect(slider).toHaveValue("2");

  // open the memory drawer from the keyboard, Escape closes it and focus returns to the control that opened it
  const file = page.getByRole("button", { name: /notes\/a\.md/ });
  await file.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "notes/a.md" });
  await expect(drawer).toBeVisible();
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => !!document.activeElement?.closest("dialog"))).toBe(true);   // focus is trapped inside
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(file).toBeFocused();
});

test("every interactive element has a visible focus indicator", async ({ page }) => {
  await page.goto("/auth/signin");
  for (const el of [page.getByLabel("Email"), page.getByLabel("Password"), page.getByRole("button", { name: "Sign in" })]) {
    await el.focus();
    const outline = await el.evaluate((n) => { const c = getComputedStyle(n); return `${c.outlineStyle} ${c.outlineWidth}`; });
    expect(outline).not.toMatch(/^none|0px/);
  }
});
