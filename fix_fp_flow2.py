content = open('frontend/public/app.html', 'r', encoding='utf-8').read()

old = """async function submitFundingPipsAccountFlow(){
  addAcctFlowState.loading=true;addAcctFlowState.error='';addAcctFlowState.success='';renderAddAccountFlow();
  try{
    const statusRes=await authFetch('/extension/status');
    if(!statusRes.ok) throw new Error('extension_status_'+statusRes.status);
    const statusData=await statusRes.json();
    const liveAccounts=statusData.accounts||{};
    const entries=Object.entries(liveAccounts);
    if(!entries.length) throw new Error('No synced FundingPips accounts detected yet. Open FundingPips with the extension first, then retry.');

    let lastAccountId='';
    for(const [acctId,acct] of entries){
      const params=new URLSearchParams({
        account_id:acctId,
        account_type:String(acct.accountType||''),
        account_size:String(acct.accountSize||0),
        label:String(acct.accountLabel||acctId),
        broker:'fundingpips',
      });
      const res=await authFetch('/auth/link-account?'+params.toString(),{method:'POST'});
      if(!res.ok) throw new Error('Could not link FundingPips account '+acctId+'.');
      const data=await res.json();
      hydrateAccountsMap(data.accounts||[],false);
      lastAccountId=acctId;
    }

    addAcctFlowState.loading=false;
    addAcctFlowState.success='FundingPips account sync complete.';
    renderAddAccountFlow();
    await refreshAfterAccountLink(lastAccountId);
    setTimeout(closeAddAccountFlow,350);
  }catch(err){
    addAcctFlowState.loading=false;
    addAcctFlowState.error=String(err?.message||'Could not link FundingPips accounts right now.');
    renderAddAccountFlow();
  }
}"""

new = """async function submitFundingPipsCredentials(){
  const email=(document.getElementById('fpEmailInput')?.value||'').trim();
  const password=(document.getElementById('fpPasswordInput')?.value||'').trim();
  if(!email||!password){
    addAcctFlowState.error='Please enter your FundingPips email and password.';
    renderAddAccountFlow();return;
  }
  addAcctFlowState.loading=true;addAcctFlowState.error='';addAcctFlowState.success='';renderAddAccountFlow();
  try{
    const res=await authFetch('/providers/prop-firm/fundingpips/connect',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email,password})
    });
    const data=await res.json();
    if(!res.ok) throw new Error(data.detail||data.error||'Connection failed.');
    addAcctFlowState.discoveredAccounts=data.accounts||[];
    addAcctFlowState.loading=false;
    if(!addAcctFlowState.discoveredAccounts.length){
      addAcctFlowState.error='Connected but no accounts found on this FundingPips profile.';
    }
    renderAddAccountFlow();
  }catch(err){
    addAcctFlowState.loading=false;
    addAcctFlowState.error=String(err?.message||'Could not connect to FundingPips right now.');
    renderAddAccountFlow();
  }
}

async function confirmFundingPipsAccounts(){
  const accts=addAcctFlowState.discoveredAccounts||[];
  const selected=accts.filter((_,i)=>document.getElementById('fp-acct-'+i)?.checked);
  if(!selected.length){
    addAcctFlowState.error='Please select at least one account.';
    renderAddAccountFlow();return;
  }
  addAcctFlowState.loading=true;addAcctFlowState.error='';renderAddAccountFlow();
  try{
    let lastId='';
    for(const a of selected){
      hydrateAccountsMap([a],false);
      lastId=a.external_account_id||a.id||'';
    }
    addAcctFlowState.loading=false;
    addAcctFlowState.success='Accounts added successfully!';
    addAcctFlowState.discoveredAccounts=[];
    renderAddAccountFlow();
    await refreshAfterAccountLink(lastId);
    setTimeout(closeAddAccountFlow,600);
  }catch(err){
    addAcctFlowState.loading=false;
    addAcctFlowState.error=String(err?.message||'Could not save accounts.');
    renderAddAccountFlow();
  }
}

async function submitFundingPipsAccountFlow(){
  await submitFundingPipsCredentials();
}"""

if old in content:
    content = content.replace(old, new)
    open('frontend/public/app.html', 'w', encoding='utf-8').write(content)
    print('SUCCESS')
else:
    print('NOT FOUND')
    if 'submitFundingPipsAccountFlow' in content:
        print('Function exists but text did not match exactly')