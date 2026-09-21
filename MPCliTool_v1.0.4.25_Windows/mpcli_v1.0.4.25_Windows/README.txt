mpcli - A command line tool for flash programming

usage: mpcli [options] ...
options:
  -V, --version                 show MPCLI Version
  -h, --help                    print this massage
  -c, --com                     com port (string [=])
  -b, --baud                    baud rate, default:1000000 (string [=1000000])
  -a, --auto                    programming binary files from json file automatically
  -f, --json                    json file, used with -a (string [=json file path])
  -P, --packetimage             Packed Image (string [=packet image file path])
  -e, --pageerase               erase sector, used with –A and –S together
  -p, --program                 used with –A and –F together
  -v, --verify                  used with –A and –S together
  -s, --savebin                 bin file pathname, used with -A ,-F and -S together
  -w, --window_dump_data        used with –A and –S together, output flash data in Command prompt window
  -m, --modify                  modify bytes in sequence, used with -A (string [= maxlength 32bytes in Hex])
  -A, --address                 address in Hex, -A 0x801000 (string [=])
  -S, --size                    size, -S 4096 (string [=])
  -F, --filepath                file path (string [=])
  -T, --set_xtal_calibration    Set the 40MHz XTAL Internal Cap Calibration value (string [=])
  -x, --set_mac                 set MAC address. (string [=])
  -n, --productid               4bytes，used with -x and -k together (string [=])
  -k, --secretkey               32bytes, used with -x and -n together (string [=])
  -u, --efuse_json              program eFuse json file(for APP encryption) (string [=])
  -U, --efuse_otp               write eFuse as OTP (string [=])
  -I, --dump_euid_mac           dump EUID and MAC
  -B, --get_back_mac            keep original MAC in Flash if download config file.used with P or f
  -M, --mandatory               Selection of program mode,1:Vendor Write mode (int [=1])
  -D, --debugpassword           debug password, -D 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF (string [=])
  -C, --erase                   chip erase but remain 4k(0x800000 - 0x801000)
  -E, --chiperase               chip erase
  -r, --reboot                  IC reboot

Two mode support:
    This tool supports two programming mode, distributed mode and packed mode. In distributed mode, you have to
    specify a json config file by the -f option, see example[1] below. In packed mode, you have to specify the
    -P option, see example[2] below. These two modes cannot be used altogether.
  
Example[1]:
  For Windows host:
    mpcli.exe -f mptoolconfig.json -c com13 -b 1000000 -a -r
    mpcli.exe -f mptoolconfig.json -c com13 -b 1000000 -e -A 0x802000 -S 0x1000 -r
    mpcli.exe -f mptoolconfig.json -c com13 -b 1000000 -e -p -v -F D:\Some\Path\To\XXX.bin -A 0x802000 -S 0x1000 -r
  For Linux host:
    sudo mpcli -f mptoolconfig.json -c /dev/ttyUSB0 -a -r
    sudo mpcli -f mptoolconfig.json -c /dev/ttyUSB0 -e -A 0x802000 -S 0x1000 -r
    sudo mpcli -f mptoolconfig.json -c /dev/ttyUSB0 -e -p -v -F /home/yourname/bin-dir/xxx.bin -A 0x802000 -S 0x1000 -r
  For MAC host:
    - The command line is different only in the port number.
Example[2]:
  For Windows host:
    mpcli.exe -P ImgPackedFile.bin -c com13 -r
  For Linux host:
    sudo mpcli -P ImgPackedFile.bin -c /dev/ttyUSB0 -r
  For MAC host:
    - The command line is different only in the port number.
Attention:
1.  The -D option can only be used with -c option altogether. The mpcli will send a HCI command containing the password following -D,
    which will result a watchdog reset.
2.  The -m option only supports modifying maximum 32 bytes in one call. If you want to modify more than 32 bytes, please call the mpcli 
    several times with appropriate values followed by -m.
3.  For Windows host, the baudrate can only be set to 1Mbps, 2Mbps or 3Mbps, other values are not supported. 1Mbps is recommended for stability.
    For Linux host, the baudrate is limited to 115200 bps temporarily for stability.