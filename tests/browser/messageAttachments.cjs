// Authenticated, disposable two-user fixture only. Never use production actors.
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/tmp/von-playwright';
const {chromium, expect}=require('@playwright/test');
const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
const output=process.argv[2] || '.run/attachments';
fs.mkdirSync(output,{recursive:true});
const png=fs.readFileSync(path.join(__dirname,'fixtures','attachment.png'));
const org='#V#attachment_fixture_organisation';
const report={method:'Local fixture authentication; synthetic clipboard/drop events carrying actual bytes; standard upload/send/read routes',messages:[],checks:[]};
async function login(browser,port,actor){const context=await browser.newContext();const page=await context.newPage();page.setDefaultTimeout(20000);await page.goto(`http://127.0.0.1:${port}/von/`);await page.locator('#vonBrowserTestLoginButton').click();await page.locator('.footer-org-menu-trigger').click();await page.getByRole('menuitemradio',{name:/Attachment Fixture Organisation/}).click();const auth=await page.evaluate(async()=> (await fetch('/von/api/auth/status')).json());assert.equal(auth.user_concept_id,actor);return page;}
async function fileEvent(page,selector,kind,name,bytes,mime){await page.locator(selector).evaluate((el,{kind,name,bytes,mime})=>{const dt=new DataTransfer();dt.items.add(new File([new Uint8Array(bytes)],name,{type:mime}));el.dispatchEvent(kind==='paste'?new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}):new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}));},{kind,name,bytes:[...bytes],mime});}
(async()=>{const browser=await chromium.launch({headless:true});try{const alice=await login(browser,5013,'#V#attachment_fixture_alice');const bob=await login(browser,5014,'#V#attachment_fixture_bob');
report.health=await alice.evaluate(async()=> (await fetch('/health')).json());
await alice.evaluate(async()=> (await import('/static/js/components/messagePanel.js')).showMessageComposer());await alice.locator('#newMessageRecipient').fill('#V#attachment_fixture_bob');
for(const [index,kind,caption] of [[0,'paste','Pasted image caption'],[1,'drop',''],[2,'file','File caption'],[3,'retry','Retry caption'],[4,'lost-response','Uncertain send caption']]){
 const fresh=index===0;const input=fresh?'#newMessageContent':'#messageInput';const root=fresh?'.message-modal-body':'#messageComposeArea';const send=fresh?'#sendNewMessage':'#sendMessageBtn';const filename=`acceptance-${Date.now()}-${index}.${kind==='file'?'txt':'png'}`;const bytes=kind==='file'?Buffer.from('Recipient evidence: sample 42.'):png;const mime=kind==='file'?'text/plain':'image/png';await alice.locator(input).fill(caption);
 if(kind==='retry'){let fail=true;await alice.route('**/api/images/upload',async route=>{if(fail){fail=false;await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Injected recoverable upload failure'})});}else await route.continue();});}
 if(kind==='paste'||kind==='drop')await fileEvent(alice,input,kind,filename,bytes,mime);else await alice.locator(`${root} input[type=file]`).setInputFiles({name:filename,mimeType:mime,buffer:bytes});
 if(kind==='retry'){await expect(alice.getByRole('button',{name:`Retry upload of ${filename}`})).toBeVisible();assert.equal(await alice.locator(input).inputValue(),caption);await alice.getByRole('button',{name:`Retry upload of ${filename}`}).click();}
 if(index===1)await alice.setViewportSize({width:360,height:800});
 await expect(alice.locator(send)).toBeVisible();await expect(alice.locator(send)).toBeEnabled();await expect(alice.locator(root)).not.toContainText(`Uploading ${filename}`);
 if(kind==='lost-response'){let lose=true;await alice.route('**/api/messages/',async route=>{if(lose){lose=false;const result=await route.fetch();assert.equal(result.status(),201);await route.abort('failed');}else await route.continue();});await alice.locator(send).click();await expect(alice.locator('#messageComposeFailure')).toBeVisible();assert.equal(await alice.locator(input).inputValue(),caption);await expect(alice.locator(root)).toContainText(filename);}
 const response=alice.waitForResponse(r=>r.url().endsWith('/api/messages/')&&r.request().method()==='POST');await alice.locator(send).click();const r=await response;const receipt=await r.json();assert.ok(r.ok(),JSON.stringify(receipt));if(kind==='lost-response')assert.equal(receipt.reused,true);report.messages.push(receipt.message_id);if(index===1)await alice.setViewportSize({width:1280,height:720});await expect(alice.locator('#messageViewContent').getByRole('link',{name:filename,exact:false})).toBeVisible();
 // Canonical recipient read-back; the owner-only URL must remain unavailable.
 const read=await bob.evaluate(async({id})=>{const r=await fetch(`/api/messages/${encodeURIComponent(id)}`);return {status:r.status,body:await r.json()};},{id:receipt.message_id});assert.equal(read.status,200);const message=read.body;const descriptor=message.concept_data.metadata.attachments[0];assert.equal(descriptor.filename,filename);
 const url=`/api/messages/${encodeURIComponent(receipt.message_id)}/attachments/${encodeURIComponent(descriptor.concept_id)}`;
 const download=await bob.request.get(`http://127.0.0.1:5014${url}`);assert.equal(download.status(),200);assert.deepEqual(await download.body(),bytes);
 assert.equal((await bob.request.get(`http://127.0.0.1:5014/von/api/images/${encodeURIComponent(descriptor.concept_id)}/original`)).status(),404);
 assert.equal((await bob.request.get(`http://127.0.0.1:5014/api/messages/${encodeURIComponent(receipt.message_id)}/attachments/%23V%23unrelated`)).status(),404);
 if(kind==='file')assert.match(download.headers()['content-disposition'],/^attachment;/);
 report.checks.push({kind,attachment_only:!caption,recipient_original_matches:true,private_owner_url_denied:true});
 await alice.unroute('**/api/images/upload');await alice.unroute('**/api/messages/');
}
await fileEvent(alice,'#messageInput','paste','remove-before-send.png',png,'image/png');await expect(alice.getByRole('button',{name:'Remove remove-before-send.png',exact:true})).toBeVisible();await alice.getByRole('button',{name:'Remove remove-before-send.png',exact:true}).click();await expect(alice.locator('#messageComposeArea')).not.toContainText('remove-before-send.png');report.checks.push({remove_before_send:true});
await alice.screenshot({path:path.join(output,'messages-desktop.png')});
await bob.reload();await bob.getByRole('button',{name:/Attachment Fixture Alice/}).first().click().catch(async()=>{const rows=await bob.evaluate(async()=> (await (await fetch('/api/messages/catalogue?all_contexts=true')).json()).conversations);assert.ok(rows.length);await bob.evaluate(async row=>(await import('/static/js/components/conversationCatalogue.js')).selectMessageConversation(row),rows[0]);});await expect(bob.locator('#messageViewContent')).toContainText('Recipient evidence', {timeout:1000}).catch(async()=>{await expect(bob.locator('#messageViewContent')).toContainText('.txt');});
for(const page of [alice,bob]){await page.setViewportSize({width:360,height:800});await expect(page.locator('#messageInput')).toBeVisible();await expect(page.locator('#messageComposeArea').getByRole('button',{name:'Attach files',exact:true})).toBeVisible();await page.screenshot({path:path.join(output,page===alice?'messages-narrow-sender.png':'messages-narrow-recipient.png')});}
report.checks.push({refresh_recipient_persistence:true,desktop_and_narrow:true});
fs.writeFileSync(path.join(output,'messages-result.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
}finally{await browser.close()}})().catch(e=>{fs.writeFileSync(path.join(output,'messages-partial.json'),JSON.stringify({...report,error:String(e)},null,2));console.error(e);process.exit(1)});
