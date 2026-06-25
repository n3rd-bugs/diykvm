Add-Type -AssemblyName System.Windows.Forms
$caps = [System.Windows.Forms.Control]::IsKeyLocked([System.Windows.Forms.Keys]::CapsLock)
$num  = [System.Windows.Forms.Control]::IsKeyLocked([System.Windows.Forms.Keys]::NumLock)
$scr  = [System.Windows.Forms.Control]::IsKeyLocked([System.Windows.Forms.Keys]::Scroll)
"CAPS=$caps NUM=$num SCROLL=$scr"
