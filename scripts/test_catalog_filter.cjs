/* Regression of the in-browser filter; uses a minimal real-DOM-compatible stub.
   Catches accidentally using querySelector instead of querySelectorAll. */
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync('static/pos-ui-v3.js','utf8');
const from=source.indexOf('  function applyCatalogFilter() {');
const until=source.indexOf('  function decorateDraftPanel()',from);
assert.ok(from>=0 && until>from, 'Filtering function not found');
const filterFunction=source.slice(from,until);
const categories=[
 {dataset:{posCategory:'*'},classList:{active:false,toggle(_c,b){this.active=b}}},
 {dataset:{posCategory:'0'},classList:{active:false,toggle(_c,b){this.active=b}}},
 {dataset:{posCategory:'1'},classList:{active:false,toggle(_c,b){this.active=b}}}
];
function product(name,category){
 return {dataset:{posCat:category},hidden:false,
 querySelector(selector){assert.equal(selector,'.pos-product-name');return{textContent:name}}};
}
const products=[product('Tacos al pastor','0'),product('Enchiladas','0'),product('Refresco','1')];
const indicator={textContent:''},empty={hidden:true};
const gallery={
 querySelectorAll(selector){
  return selector==='.pos-category'?categories:selector==='.pos-product'?products:[];
 },
 querySelector(selector){
  if(selector==='.pos-product-count')return indicator;
  if(selector==='.pos-search-empty')return empty;
  return null;
 }
};
function run(selectedCategory,catalogSearch,visible){
 const ctx={selectedCategory,catalogSearch,$:(s)=>s==='#pos-catalog'?gallery:null,
 $$:(s,root)=>root.querySelectorAll(s)};
 vm.runInNewContext(filterFunction+'\napplyCatalogFilter()',ctx);
 assert.deepEqual(products.map(p=>!p.hidden),visible);
 assert.equal(categories.filter(c=>c.classList.active).length,1);
 assert.equal(empty.hidden,visible.some(Boolean));
}
run('*','',[true,true,true]);
run('0','',[true,true,false]);
run('1','',[false,false,true]);
run('*','tacos',[true,false,false]);
run('0','enchiladas',[false,true,false]);
run('1','tacos',[false,false,false]);
console.log('OK: catálogo responde a categorías y búsqueda sin consultar API');
