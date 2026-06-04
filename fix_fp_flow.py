content = open('frontend/public/app.html', 'r', encoding='utf-8').read()

old = """  if(addAcctFlowState.path==='fundingpips'){
    content=`<div class="aaf-flow-note">Open FundingPips in your browser with the TaliTrade extension active, then run a sync scan below. We only link accounts already detected by <code>/extension/status</code>.</div>
      <div class="aaf-option" style="margin-bottom:0"><div class="aaf-option-top"><div class="aaf-option-title">Extension-based linking</div><span class="aaf-functional">linked in-shell</span></div>
      <div class="aaf-option-desc">This keeps onboarding native to the legacy shell and reuses your authenticated account-link endpoint.</div></div>`;
    footer.innerHTML=`<button class="btn btn-ghost rip" onclick="backAddAccountStep()" ${addAcctFlowState.loading?'disabled':''}>Back</button>
      <button class="btn btn-blue rip" onclick="submitFundingPipsAccountFlow()" ${addAcctFlowState.loading?'disabled':''}>${addAcctFlowState.loading?'Linking\u2026':'Scan & link accounts'}</button>`;"""

new = """  if(addAcctFlowState.path==='fundingpips'){
    if(addAcctFlowState.discoveredAccounts&&addAcctFlowState.discoveredAccounts.length){
      const accts=addAcctFlowState.discoveredAccounts;
      let acctHtml='<div class="aaf-flow-note">Select the accounts you want to add to TaliTrade.</div><div class="aaf-form">';
      accts.forEach((a,i)=>{
        acctHtml+=`<label style="display:flex;align-items:center;gap:10px;padding:10px;background:var(--s2);border-radius:8px;margin-bottom:6px;cursor:pointer"><input type="checkbox" id="fp-acct-${i}" value="${a.external_account_id}" checked style="width:16px;height:16px;accent-color:var(--blue)"><div><div style="font-size:13px;font-weight:500">${a.display_label}</div><div style="font-size:11px;color:var(--t3)">${a.account_type||''} ${a.account_size?'$'+a.account_size.toLocaleString():''}</div></div></label>`;
      });
      acctHtml+='</div>';
      content=acctHtml;
      footer.innerHTML=`<button class="btn btn-ghost rip" onclick="backAddAccountStep()">Back</button><button class="btn btn-blue rip" onclick="confirmFundingPipsAccounts()" ${addAcctFlowState.loading?'disabled':''}>${addAcctFlowState.loading?'Saving...':'Add selected accounts'}</button>`;
    } else {
      content=`<div class="aaf-flow-note">Enter your FundingPips login credentials. TaliTrade will securely connect and import your trading accounts.</div><div class="aaf-form"><label><div class="aaf-field-label">Email</div><input class="aaf-input" id="fpEmailInput" type="email" placeholder="your@email.com" autocomplete="email"></label><label><div class="aaf-field-label">Password</div><input class="aaf-input" id="fpPasswordInput" type="password" placeholder="Your FundingPips password" autocomplete="current-password"></label></div>`;
      footer.innerHTML=`<button class="btn btn-ghost rip" onclick="backAddAccountStep()" ${addAcctFlowState.loading?'disabled':''}>Back</button><button class="btn btn-blue rip" onclick="submitFundingPipsCredentials()" ${addAcctFlowState.loading?'disabled':''}>${addAcctFlowState.loading?'Connecting...':'Connect FundingPips'}</button>`;
    }"""

if old in content:
    content = content.replace(old, new)
    open('frontend/public/app.html', 'w', encoding='utf-8').write(content)
    print('SUCCESS')
else:
    print('NOT FOUND - checking for partial match...')
    if 'Extension-based linking' in content:
        print('Partial match found')
    else:
        print('No match at all')