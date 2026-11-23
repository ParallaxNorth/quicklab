#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Configure one Windows guest on the isolated Quicklab LAN.
.DESCRIPTION
Run from an elevated 64-bit Windows PowerShell console inside the VM, not on
its host. The ISO is never written to. When told to restart, restart the VM
and rerun the same Setup command. Finish DC Setup before starting Employee
Setup. Use the built-in Administrator with a strong password for the first
forest promotion, then sign in as LAB\Administrator after that restart. The
DSRM password is separate. CreateLabUser never changes guest settings.
.PARAMETER Automation
Setup only. Run in an elevated, noninteractive Windows PowerShell process.
Set QUICKLAB_DSRM_PASSWORD for forest promotion and QUICKLAB_JOIN_USER and
QUICKLAB_JOIN_PASSWORD for the employee join. Supply the join variables on the
DC readiness run if AD validation needs explicit credentials (e.g. under the
QEMU guest agent's LocalSystem account). Use lab-only credentials. Setup clears
these variables from its own process; supply them again on runs that need them.
Exit 3010 means Quicklab staged a change in this boot; the caller must reboot
and rerun. Setup never reboots the guest. An unrelated pending Windows rename
or servicing reboot is an error, not 3010; investigate it before rerunning.
.EXAMPLE
.\setup-windows.ps1 -Role DC
.EXAMPLE
.\setup-windows.ps1 -Role Employee
.EXAMPLE
.\setup-windows.ps1 -Role DC -Action CreateLabUser -UserName lab-user
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('DC', 'Employee')]
    [string]$Role,

    [ValidateSet('Setup', 'CreateLabUser')]
    [string]$Action = 'Setup',

    [ValidatePattern('^[a-zA-Z][a-zA-Z0-9-]{0,19}\z')]
    [string]$UserName,

    [switch]$Automation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Action -ne 'Setup' -and $PSBoundParameters.ContainsKey('Automation')) {
    throw '-Automation is only for Setup.'
}
$automationDsrm = $null
$automationJoin = $null
if ($Automation) {
    try {
        if ($Role -eq 'DC') { $rawDsrm = $env:QUICKLAB_DSRM_PASSWORD }
        $joinUser = $env:QUICKLAB_JOIN_USER
        $rawJoinPassword = $env:QUICKLAB_JOIN_PASSWORD
        foreach ($name in @('QUICKLAB_DSRM_PASSWORD', 'QUICKLAB_JOIN_USER', 'QUICKLAB_JOIN_PASSWORD')) {
            [Environment]::SetEnvironmentVariable($name, [NullString]::Value, 'Process')
        }
        if ($Role -eq 'DC' -and -not [string]::IsNullOrEmpty($rawDsrm)) {
            $automationDsrm = ConvertTo-SecureString $rawDsrm -AsPlainText -Force
        }
        if (-not [string]::IsNullOrWhiteSpace($joinUser) -and -not [string]::IsNullOrEmpty($rawJoinPassword)) {
            $joinPassword = ConvertTo-SecureString $rawJoinPassword -AsPlainText -Force
            $automationJoin = [pscredential]::new($joinUser, $joinPassword)
        }
    } catch {
        throw 'Could not read and clear the Quicklab automation credentials from the process environment.'
    } finally {
        $rawDsrm = $joinUser = $rawJoinPassword = $null
    }
}

if ($PSVersionTable.PSEdition -ne 'Desktop' -or -not [Environment]::Is64BitProcess) {
    throw 'Use elevated 64-bit Windows PowerShell (powershell.exe), not PowerShell Core.'
}
if ($Action -eq 'CreateLabUser' -and ($Role -ne 'DC' -or -not $UserName)) {
    throw 'CreateLabUser requires -Role DC and -UserName (1-20 letters, digits or hyphens, starting with a letter).'
}
if ($Action -eq 'Setup' -and $PSBoundParameters.ContainsKey('UserName')) {
    throw '-UserName is only for CreateLabUser.'
}

$domainName = 'lab.test'
$netbiosName = 'LAB'
$dcName = 'ql-dc.lab.test'
$dcIp = '10.77.0.10'
$prefixLength = 24
if ($Role -eq 'DC') {
    $target = @{ Name = 'ql-dc'; IP = $dcIp; MAC = '525400770010' }
} else {
    $target = @{ Name = 'ql-win11'; IP = '10.77.0.20'; MAC = '525400770020' }
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ($Role -eq 'DC') {
    if ($os.ProductType -notin @(2, 3) -or [int]$os.BuildNumber -ne 20348) {
        throw 'The DC role requires Windows Server 2022.'
    }
} elseif ($os.ProductType -ne 1 -or [int]$os.BuildNumber -lt 22000 -or $os.OperatingSystemSKU -ne 48) {
    throw 'The Employee role requires Windows 11 Pro (not Pro N or other editions).'
}

$adapters = @(Get-NetAdapter)
if ($adapters.Count -ne 1 -or -not $adapters[0].HardwareInterface -or
    ($adapters[0].MacAddress -replace '[:-]', '') -ne $target.MAC) {
    throw "Expected exactly one visible hardware NIC with MAC $($target.MAC) for $Role. Check the VM and its VirtIO driver. No network changes made."
}
$nic = $adapters[0]
if ($nic.Status -ne 'Up') {
    throw "Lab NIC '$($nic.Name)' is not up. Check its connection to the private LAN."
}
$nicIndex = $nic.InterfaceIndex

# A join or promotion can be staged while Windows still reports the old role.
$stateKey = 'HKLM:\SOFTWARE\Quicklab\GuestSetup'
$bootId = $os.LastBootUpTime.ToUniversalTime().ToString('o')
function Write-RestartRequired {
    param([string]$Change)

    if (-not (Test-Path -LiteralPath $stateKey)) {
        New-Item -Path $stateKey -Force | Out-Null
    }
    Set-ItemProperty -LiteralPath $stateKey -Name 'RestartRequestedInBoot' -Value $bootId
    if ($Automation) {
        Write-Host "$Change A guest restart is required."
    } else {
        Write-Host "$Change Restart this guest (Restart-Computer), then rerun: .\setup-windows.ps1 -Role $Role"
    }
}

if (Test-Path -LiteralPath $stateKey) {
    $marker = (Get-ItemProperty -LiteralPath $stateKey).PSObject.Properties['RestartRequestedInBoot']
    if ($null -ne $marker -and $marker.Value -eq $bootId) {
        if ($Automation) {
            Write-Host "A Quicklab change is awaiting a restart for $Role."
            exit 3010
        }
        throw "A Quicklab change is awaiting a restart. Restart this guest, then rerun Setup for $Role."
    }
}
$nameKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName'
$activeName = Get-ItemPropertyValue -LiteralPath "$nameKey\ActiveComputerName" -Name ComputerName
$pendingName = Get-ItemPropertyValue -LiteralPath "$nameKey\ComputerName" -Name ComputerName
if ($activeName -ne $pendingName -or
    (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')) {
    throw 'Windows has a pending rename or servicing restart. Restart this guest before rerunning Setup.'
}

$isDc = $computer.DomainRole -in @(4, 5)
$inDomain = $isDc -or $computer.PartOfDomain
if ($inDomain -and ($computer.Domain -ne $domainName -or $activeName -ne $target.Name -or
    $isDc -ne ($Role -eq 'DC'))) {
    throw 'This guest already belongs to a domain with a different name, host name or role than expected. Refusing to rename, rejoin or promote it.'
}
if ($Action -eq 'CreateLabUser' -and -not $isDc) {
    throw 'Finish DC Setup and restart before creating a lab user.'
}

function Get-LabIPv4 {
    param([string]$Store = 'ActiveStore')

    Get-NetIPAddress -PolicyStore $Store | Where-Object { $_.InterfaceIndex -eq $nicIndex -and $_.AddressFamily -eq 'IPv4' }
}

function Get-DefaultRoutes {
    param([string]$Store)

    Get-NetRoute -PolicyStore $Store | Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0', '::/0') }
}

function Assert-LabAddressing {
    $ipInterface = Get-NetIPInterface -InterfaceIndex $nicIndex -AddressFamily IPv4
    $addresses = @(Get-LabIPv4)
    if ($ipInterface.Dhcp -ne 'Disabled' -or $addresses.Count -ne 1 -or
        $addresses[0].IPAddress -ne $target.IP -or $addresses[0].PrefixLength -ne $prefixLength -or
        $addresses[0].PrefixOrigin -ne 'Manual' -or $addresses[0].SkipAsSource -or
        $addresses[0].AddressState -ne 'Preferred') {
        throw "Expected one usable static IPv4 address ($($target.IP)/$prefixLength) on '$($nic.Name)'. Check for an address conflict or unexpected IP settings."
    }
    if (@(Get-DefaultRoutes -Store ActiveStore).Count -gt 0 -or
        @(Get-DefaultRoutes -Store PersistentStore).Count -gt 0) {
        throw 'A default route remains. Check the VM network and gateway settings; this lab must have no default gateway.'
    }
}

function Assert-LabIpv6Dns {
    # Permit the DC's own IPv6 DNS loopback, which AD DS can install.
    $unexpected = @(Get-DnsClientServerAddress -InterfaceIndex $nicIndex -AddressFamily IPv6 |
        ForEach-Object { $_.ServerAddresses } | Where-Object { $_ -and -not ($Role -eq 'DC' -and $_ -eq '::1') })
    if ($unexpected.Count -gt 0) {
        throw 'Unexpected IPv6 DNS server on the lab NIC. Remove it or isolate the private LAN before running this script.'
    }
}

function Assert-LabDns {
    $dns = @(Get-DnsClientServerAddress -InterfaceIndex $nicIndex -AddressFamily IPv4 |
        ForEach-Object { $_.ServerAddresses } | Where-Object { $_ })
    if ($dns.Count -ne 1 -or $dns[0] -ne $dcIp) {
        throw "Expected $dcIp as the only IPv4 DNS server on the lab NIC."
    }
    Assert-LabIpv6Dns
}

function Set-LabNetwork {
    Assert-LabIpv6Dns
    $ipInterface = Get-NetIPInterface -InterfaceIndex $nicIndex -AddressFamily IPv4
    $knownIps = @(Get-LabIPv4; Get-LabIPv4 -Store PersistentStore)
    $unexpectedIps = @($knownIps | Where-Object {
        $_.PrefixOrigin -ne 'Dhcp' -and
        -not ($_.IPAddress -like '169.254.*' -and $_.PrefixOrigin -ne 'Manual') -and
        ($_.IPAddress -ne $target.IP -or $_.PrefixLength -ne $prefixLength -or
            $_.PrefixOrigin -ne 'Manual' -or $_.SkipAsSource)
    })
    $unexpectedRoutes = @(Get-DefaultRoutes -Store ActiveStore | Where-Object {
        -not ($_.InterfaceIndex -eq $nicIndex -and $_.DestinationPrefix -eq '0.0.0.0/0' -and
            $_.Protocol -eq 'Dhcp' -and $ipInterface.Dhcp -eq 'Enabled')
    })
    if ($unexpectedIps.Count -gt 0 -or $unexpectedRoutes.Count -gt 0 -or
        @(Get-DefaultRoutes -Store PersistentStore).Count -gt 0) {
        throw 'Unexpected IPv4 address or default route. Review the guest networking first; no addresses or routes were removed.'
    }

    # Disabling DHCP retires its lease and gateway. Never delete unrelated routes or addresses.
    if ($ipInterface.Dhcp -ne 'Disabled') {
        Set-NetIPInterface -InterfaceIndex $nicIndex -AddressFamily IPv4 -Dhcp Disabled
    }
    $staticIp = @(Get-LabIPv4 | Where-Object { $_.IPAddress -eq $target.IP -and $_.PrefixOrigin -eq 'Manual' })
    if ($staticIp.Count -eq 0) {
        New-NetIPAddress -InterfaceIndex $nicIndex -AddressFamily IPv4 -IPAddress $target.IP -PrefixLength $prefixLength | Out-Null
    }
    # Wait briefly for duplicate-address detection and DHCP route cleanup before setting DNS.
    for ($i = 0; $i -lt 15; $i++) {
        $ip = @(Get-LabIPv4 | Where-Object { $_.IPAddress -eq $target.IP })
        if ($ip.Count -eq 1 -and $ip[0].AddressState -eq 'Duplicate') {
            throw "Address $($target.IP) is already in use on the LAN. Resolve the conflict and rerun Setup."
        }
        if ($ip.Count -eq 1 -and $ip[0].AddressState -eq 'Preferred' -and
            @(Get-DefaultRoutes -Store ActiveStore).Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    Assert-LabAddressing
    Set-DnsClientServerAddress -InterfaceIndex $nicIndex -ServerAddresses @($dcIp)
    Assert-LabDns
}

function Assert-DcDns {
    try {
        $srv = @(Resolve-DnsName -Name "_ldap._tcp.dc._msdcs.${domainName}." -Type SRV -Server $dcIp -DnsOnly -NoHostsFile |
            Where-Object { $_.Type -eq 'SRV' -and $_.NameTarget.TrimEnd('.') -eq $dcName -and $_.Port -eq 389 })
        $hostAddresses = @(Resolve-DnsName -Name "${dcName}." -Type A -Server $dcIp -DnsOnly -NoHostsFile |
            Where-Object { $_.Type -eq 'A' } | Select-Object -ExpandProperty IPAddress -Unique)
        if ($srv.Count -eq 0 -or $hostAddresses.Count -ne 1 -or $hostAddresses[0] -ne $dcIp) {
            throw 'Expected LDAP SRV and DC A records were not found.'
        }
    } catch {
        throw "AD DNS is not ready at $dcIp. Finish DC Setup, restart, and check the DC's DNS registration and private LAN. $($_.Exception.Message)"
    }
}

function Get-ReadyLabDomain {
    Import-Module ActiveDirectory
    if ($Automation) {
        # The guest agent can come up before DNS and AD Web Services after promotion.
        $deadline = [DateTime]::UtcNow.AddMinutes(2)
        try {
            while ($true) {
                try {
                    Assert-DcDns
                    if ($automationJoin) {
                        $adDomain = Get-ADDomain -Server $dcName -Identity $domainName -Credential $automationJoin
                    } else {
                        $adDomain = Get-ADDomain -Server $dcName -Identity $domainName
                    }
                    break
                } catch {
                    if ([DateTime]::UtcNow -ge $deadline) {
                        throw "Could not validate $domainName at $dcName. Check AD DNS, AD Web Services and the guest agent's identity."
                    }
                    Start-Sleep -Seconds 5
                }
            }
        } finally {
            if ($automationJoin) { $automationJoin.Password.Dispose() }
        }
    } else {
        Assert-DcDns
        $adDomain = Get-ADDomain -Server $dcName
    }
    if ($adDomain.DNSRoot -ne $domainName -or $adDomain.NetBIOSName -ne $netbiosName) {
        throw "Expected the $domainName domain with NetBIOS name $netbiosName."
    }
    $adDomain
}

if ($Action -eq 'CreateLabUser') {
    Assert-LabAddressing
    Assert-LabDns
    $adDomain = Get-ReadyLabDomain
    $upn = "$UserName@$domainName"
    # UserName's allowed characters are also safe in this LDAP filter.
    $existing = Get-ADObject -Server $dcName -SearchBase $adDomain.DistinguishedName `
        -LDAPFilter "(|(sAMAccountName=$UserName)(userPrincipalName=$upn))" -ResultSetSize 1
    if ($existing) {
        throw "An AD object already uses $UserName or $upn. No account changes made."
    }
    $password = Read-Host "Initial password for $upn (must meet domain policy)" -AsSecureString
    try {
        if ($password.Length -eq 0) { throw 'A nonempty password is required.' }
        $user = New-ADUser -Server $dcName -Path $adDomain.UsersContainer -Name $UserName `
            -SamAccountName $UserName -UserPrincipalName $upn -Enabled $false -PassThru
        try {
            Set-ADAccountPassword -Server $dcName -Identity $user -Reset -NewPassword $password
            Set-ADUser -Server $dcName -Identity $user -ChangePasswordAtLogon $true
            Enable-ADAccount -Server $dcName -Identity $user
        } catch {
            throw "Could not finish provisioning $upn. Inspect the new account (created disabled); Setup will not modify an existing user. $($_.Exception.Message)"
        }
    } finally {
        $password.Dispose()
    }
    Write-Host "Created standard user $upn (no privileged groups added). Password change is required at first sign-in."
    return
}

Set-LabNetwork
if ($activeName -ne $target.Name) {
    Rename-Computer -NewName $target.Name -Force
    Write-RestartRequired "Renamed guest to $($target.Name)."
    if ($Automation) { exit 3010 }
    return
}

if ($Role -eq 'DC') {
    if ($isDc) {
        $null = Get-ReadyLabDomain
        Write-Host "DC ready: $dcName on $dcIp/$prefixLength, domain $domainName ($netbiosName)."
        return
    }

    Import-Module ServerManager
    $feature = Install-WindowsFeature -Name AD-Domain-Services -IncludeManagementTools
    if (-not $feature.Success) { throw "AD DS role installation failed: $($feature.ExitCode)." }
    if ($feature.RestartNeeded -ne 'No') {
        Write-RestartRequired 'Windows requested a restart during AD DS role installation.'
        if ($Automation) { exit 3010 }
        return
    }
    Import-Module ADDSDeployment
    if ($Automation) {
        if (-not $automationDsrm) { throw 'Set QUICKLAB_DSRM_PASSWORD for forest promotion.' }
        $dsrm = $automationDsrm
    } else {
        $dsrm = Read-Host 'Set a strong Directory Services Restore Mode (DSRM) password' -AsSecureString
    }
    try {
        if ($dsrm.Length -eq 0) { throw 'A nonempty DSRM password is required.' }
        Install-ADDSForest -DomainName $domainName -DomainNetbiosName $netbiosName `
            -InstallDns -CreateDnsDelegation:$false -SafeModeAdministratorPassword $dsrm `
            -NoRebootOnCompletion:$true -Force | Out-Host
    } catch {
        if ($Automation) {
            throw 'Forest promotion failed. Check the DSRM password policy and AD DS deployment logs in the guest.'
        }
        throw
    } finally {
        $dsrm.Dispose()
    }
    Write-RestartRequired "Forest promotion staged for $domainName."
    if ($Automation) { exit 3010 }
    return
}

Assert-DcDns
if ($inDomain) {
    if (-not (Test-ComputerSecureChannel -Server $dcName)) {
        throw 'The computer is joined but its secure channel is not healthy. The script will not reset or rejoin it.'
    }
    Write-Host "Employee ready: $($target.Name) on $($target.IP)/$prefixLength, joined to $domainName."
    return
}

if ($Automation) {
    if (-not $automationJoin) { throw 'Set QUICKLAB_JOIN_USER and QUICKLAB_JOIN_PASSWORD for the domain join.' }
    $credential = $automationJoin
} else {
    $credential = Get-Credential -Message "Enter a $netbiosName domain credential authorized to join $($target.Name) to $domainName."
    if (-not $credential) { throw 'No join credential supplied.' }
}
try {
    $join = Add-Computer -DomainName $domainName -Server $dcName -Credential $credential -Force -PassThru
    if (-not $join.HasSucceeded) { throw "The join to $domainName did not succeed." }
} catch {
    if ($Automation) {
        throw "The join to $domainName failed. Check the lab credential's join permission and the guest's NetSetup log."
    }
    throw
} finally {
    $credential.Password.Dispose()
}
Write-RestartRequired "Domain join staged for $domainName."
if ($Automation) { exit 3010 }
