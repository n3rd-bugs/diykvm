Add-Type -AssemblyName System.Windows.Forms
$pb = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
$p  = [System.Windows.Forms.Cursor]::Position
"PRIMARY={0}x{1} VIRTUAL={2}x{3}@{4},{5} CURSOR={6},{7}" -f $pb.Width,$pb.Height,$vs.Width,$vs.Height,$vs.X,$vs.Y,$p.X,$p.Y
