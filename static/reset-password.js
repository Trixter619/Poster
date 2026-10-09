'use strict';
const field=document.querySelector('#resetToken');
if(field&&location.hash){field.value=location.hash.slice(1);history.replaceState(null,'',location.pathname);}
