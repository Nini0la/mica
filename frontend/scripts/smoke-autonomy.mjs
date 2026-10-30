// Manual real-browser acceptance against the isolated API smoke server.
import assert from "node:assert/strict";
import {createRequire} from "node:module";
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE_PATH??"playwright");
const browser=await chromium.launch({headless:true});
try {
  const context=await browser.newContext({viewport:{width:390,height:844}});
  const login=await context.request.post("http://127.0.0.1:8012/api/auth/login",{
    headers:{origin:"http://127.0.0.1:3002"},
    data:{access_code:process.env.ACCESS_CODE,username:process.env.MICA_SMOKE_USERNAME},
  });
  assert.equal(login.status(),200);
  const page=await context.newPage();
  const errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  await page.goto("http://127.0.0.1:3002");
  await page.getByRole("button",{name:"Today",exact:true}).click();
  await page.getByRole("button",{name:"Open autonomy controls"}).click();
  await page.getByText(/Status: completed/).waitFor();
  await page.getByRole("button",{name:"Show corrections"}).click();
  await page.getByText("Correction evidence",{exact:true}).waitFor();
  await page.getByRole("button",{name:"Reopen",exact:true}).click();
  await page.getByText(/Status: open/).waitFor();
  await page.getByRole("button",{name:"Pause",exact:true}).click();
  await page.getByText(/Status: paused/).waitFor();
  await page.getByRole("button",{name:"Resume",exact:true}).click();
  await page.getByText(/Status: open/).waitFor();
  await page.getByRole("button",{name:"Cancel commitment",exact:true}).click();
  await page.getByText(/Status: cancelled/).waitFor();
  await page.getByText(/Execution history/).click();
  await page.getByText(/delivery: in_app/).first().waitFor();
  assert.deepEqual(errors,[]);
  const fits=await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth);
  assert(fits,"Autonomy controls overflow mobile viewport");
  await page.reload();
  await page.getByRole("button",{name:"Today",exact:true}).click();
  await page.getByRole("button",{name:"Open autonomy controls"}).click();
  await page.getByText(/Status: cancelled/).waitFor();
  console.log("Browser PASS: current state, corrections, reopen/pause/resume/cancel, in-app evidence, reload, mobile width");
} finally {
  await browser.close();
}
