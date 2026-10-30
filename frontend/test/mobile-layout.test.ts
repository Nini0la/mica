import {readFileSync} from "node:fs";
import {expect,it} from "vitest";

it("keeps the mobile sticky composer above the navigation and reserves the safe area",()=>{
  // JSDOM cannot measure layout or reliably parse env() in calc(). Guard the CSS contract.
  const css=readFileSync("app/style.css","utf8");
  const mobile=css.slice(css.lastIndexOf("@media(max-width:760px)"));
  const declaration=(selector:string,property:string)=>mobile.split(`${selector}{`)[1]?.split("}")[0].replace(/\s/g,"").split(";").find(item=>item.startsWith(`${property}:`))?.slice(property.length+1);
  expect(declaration(":root","--mobile-nav-height")).toBe("72px");
  expect(declaration(":root","--mobile-bottom-clearance")).toBe("calc(var(--mobile-nav-height)+env(safe-area-inset-bottom,0px))");
  expect(declaration(".sidebar","height")).toBe("var(--mobile-bottom-clearance)");
  expect(declaration(".sidebar","padding-bottom")).toContain("env(safe-area-inset-bottom,0px)");
  expect(declaration(".content","padding-bottom")).toBe("calc(var(--mobile-bottom-clearance)+20px)");
  expect(declaration(".chat-view .composer","position")).toBe("sticky");
  expect(declaration(".chat-view .composer","bottom")).toBe("calc(var(--mobile-bottom-clearance)+.5rem)");
  expect(declaration(".chat-view .composer","padding")).toBe(".85rem");
  expect(declaration(".chat-view .composer","z-index")).toBe("10");
});
