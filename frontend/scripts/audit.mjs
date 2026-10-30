import {spawnSync} from "node:child_process";

const allowedPackages=new Set([
  "braces",
  "micromatch",
  "fast-glob",
  "@next/eslint-plugin-next",
  "eslint-config-next",
]);
const allowedAdvisory="https://github.com/advisories/GHSA-vfj7-8cjw-p6xm";

function audit(extraArgs=[]){
  const result=spawnSync("npm",["audit","--json",...extraArgs],{encoding:"utf8"});
  if(result.error)throw result.error;
  let report;
  try{report=JSON.parse(result.stdout)}
  catch{throw new Error(`npm audit returned invalid JSON: ${result.stderr||result.stdout}`)}
  if(result.signal||![0,1].includes(result.status)||report.error||
      typeof report.metadata?.vulnerabilities?.total!=="number"||
      !report.vulnerabilities||typeof report.vulnerabilities!=="object"){
    throw new Error(`npm audit did not return a complete report: ${result.stderr||result.stdout}`);
  }
  const findingCount=Object.keys(report.vulnerabilities).length;
  if((findingCount===0&&result.status!==0)||(findingCount>0&&result.status!==1)){
    throw new Error(`npm audit exit status ${result.status} contradicts ${findingCount} findings`);
  }
  return report;
}

const production=audit(["--omit=dev"]);
if(production.metadata?.vulnerabilities?.total!==0){
  console.error("Production dependency audit contains vulnerabilities.");
  process.exit(1);
}

const full=audit();
const findings=Object.entries(full.vulnerabilities??{});
const unexpected=[];
for(const [name,finding] of findings){
  if(!allowedPackages.has(name)){
    unexpected.push(`${name}: package is not allowlisted`);
    continue;
  }
  if(finding.severity!=="high")unexpected.push(`${name}: unexpected severity ${finding.severity}`);
  for(const cause of finding.via??[]){
    if(typeof cause==="string"){
      if(!allowedPackages.has(cause))unexpected.push(`${name}: unexpected dependency cause ${cause}`);
    }else if(cause.name!=="braces"||cause.url!==allowedAdvisory){
      unexpected.push(`${name}: unexpected advisory ${cause.url??cause.name??"unknown"}`);
    }
  }
}
if(unexpected.length){
  console.error("Dependency audit contains findings outside the scoped braces waiver:");
  for(const message of unexpected)console.error(`- ${message}`);
  process.exit(1);
}
console.log(findings.length
  ? `Production audit clean; accepted ${findings.length} dev-tooling findings derived only from ${allowedAdvisory}.`
  : "Full dependency audit clean; the former braces waiver is no longer needed.");
