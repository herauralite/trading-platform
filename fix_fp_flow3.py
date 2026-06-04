content = open('frontend/public/app.html', 'r', encoding='utf-8').read()

old = """function openAddAccountFlow(){"""

new = """function openAddAccountFlow(){
  addAcctFlowState.discoveredAccounts=[];"""

if old in content:
    content = content.replace(old, new, 1)
    open('frontend/public/app.html', 'w', encoding='utf-8').write(content)
    print('SUCCESS')
else:
    print('NOT FOUND')