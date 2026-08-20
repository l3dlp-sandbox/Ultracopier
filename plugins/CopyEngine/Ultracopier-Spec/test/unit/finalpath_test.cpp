/** \file finalpath_test.cpp
\brief Standalone unit test for TransferThread::toFinalPath() -- the Win32 extended-path
prefixer every filesystem call in the engine goes through.

Two properties are asserted, on BOTH overloads (std::string = 8-bit and std::wstring = 16-bit,
"the same logic in 16 bits"):

  1. PREFIXING. A local path gets \\?\, a UNC/NAS path gets \\?\UNC\, a bare drive gets its
     trailing backslash, and forward slashes are normalised to backslashes. This is what lifts
     the MAX_PATH (260) limit: without it CreateFileW fails with ERROR_PATH_NOT_FOUND(3) on a
     longer path (the "path not found" user report), so a path over 260 chars must come out
     prefixed and INTACT.

  2. LENGTH-DELIMITED FIDELITY (no strlen). Paths are carried as std::string/std::wstring and
     must keep their exact SIZE: a buffer holding an embedded 0x00 ("XXXXX\0XXXX") must be
     transformed byte-for-byte, never cut at the NUL and never mis-sized. The strongest form of
     the check is a '/' AFTER the embedded NUL: an implementation that used strlen()/c_str()
     internally would stop scanning at the NUL and leave that separator unconverted, whereas the
     length-delimited one converts it. (A NUL cannot exist in a real on-disk name -- this is an
     internal data-handling invariant, the same split as weird_names.py / pathtree_test.cpp
     make for the transfer list.)

Windows-only, because toFinalPath() itself is #ifdef Q_OS_WIN32: built with the MXE mingw
toolchain and RUN ON THE REAL WINDOWS BOX by cases/finalpath_unit.py.

Exit code 0 == all checks passed; nonzero == at least one failure. */

#include <cstdio>
#include <cstdint>
#include <string>
#include "TransferThread.h"

static int failures=0;
static int checks=0;

static std::string dump(const std::string &s)
{
    std::string out;
    for(size_t i=0;i<s.size();i++)
    {
        const unsigned char c=(unsigned char)s.at(i);
        if(c==0)
            out+="\\0";
        else if(c<0x20 || c>0x7e)
        {
            char b[8];
            snprintf(b,sizeof(b),"\\x%02x",c);
            out+=b;
        }
        else
            out+=(char)c;
    }
    return out;
}

static std::string dump(const std::wstring &s)
{
    std::string out;
    for(size_t i=0;i<s.size();i++)
    {
        const wchar_t c=s.at(i);
        if(c==0)
            out+="\\0";
        else if(c<0x20 || c>0x7e)
        {
            char b[10];
            snprintf(b,sizeof(b),"\\x%04x",(unsigned int)c);
            out+=b;
        }
        else
            out+=(char)c;
    }
    return out;
}

static void check(const bool ok,const char * const what)
{
    checks++;
    if(!ok)
    {
        failures++;
        printf("FAIL: %s\n",what);
    }
}

template<class T> static void check_eq(const T &got,const T &expected,const char * const what)
{
    checks++;
    if(got!=expected)
    {
        failures++;
        printf("FAIL: %s\n  got:      \"%s\" (size %u)\n  expected: \"%s\" (size %u)\n",
               what,dump(got).c_str(),(unsigned int)got.size(),
               dump(expected).c_str(),(unsigned int)expected.size());
    }
}

// ---------------------------------------------------------------------------------------
// 1) prefixing rules (8-bit and 16-bit)
// ---------------------------------------------------------------------------------------
static void test_prefix()
{
    check_eq(TransferThread::toFinalPath(std::string("C:\\dir\\file.txt")),
             std::string("\\\\?\\C:\\dir\\file.txt"),"char: local path gets \\\\?\\");
    check_eq(TransferThread::toFinalPath(std::wstring(L"C:\\dir\\file.txt")),
             std::wstring(L"\\\\?\\C:\\dir\\file.txt"),"wchar: local path gets \\\\?\\");

    check_eq(TransferThread::toFinalPath(std::string("C:/dir/file.txt")),
             std::string("\\\\?\\C:\\dir\\file.txt"),"char: forward slashes normalised");
    check_eq(TransferThread::toFinalPath(std::wstring(L"C:/dir/file.txt")),
             std::wstring(L"\\\\?\\C:\\dir\\file.txt"),"wchar: forward slashes normalised");

    check_eq(TransferThread::toFinalPath(std::string("\\\\nas\\share\\f.txt")),
             std::string("\\\\?\\UNC\\nas\\share\\f.txt"),"char: UNC path gets \\\\?\\UNC\\");
    check_eq(TransferThread::toFinalPath(std::wstring(L"\\\\nas\\share\\f.txt")),
             std::wstring(L"\\\\?\\UNC\\nas\\share\\f.txt"),"wchar: UNC path gets \\\\?\\UNC\\");

    check_eq(TransferThread::toFinalPath(std::string("C:")),
             std::string("\\\\?\\C:\\"),"char: bare drive gets its trailing backslash");
    check_eq(TransferThread::toFinalPath(std::wstring(L"C:")),
             std::wstring(L"\\\\?\\C:\\"),"wchar: bare drive gets its trailing backslash");
}

// ---------------------------------------------------------------------------------------
// 2) a path PAST MAX_PATH survives intact (the reason the prefix exists at all)
// ---------------------------------------------------------------------------------------
static void test_over_max_path()
{
    const std::string longp=std::string("C:\\")+std::string(400,'a')+"\\f.txt";
    const std::string got=TransferThread::toFinalPath(longp);
    check(got.size()==longp.size()+4,"char: >MAX_PATH path keeps its length (+4 for the prefix)");
    check_eq(got,std::string("\\\\?\\")+longp,"char: >MAX_PATH path is prefixed, not truncated");

    const std::wstring longw=std::wstring(L"C:\\")+std::wstring(400,L'a')+L"\\f.txt";
    const std::wstring gotw=TransferThread::toFinalPath(longw);
    check(gotw.size()==longw.size()+4,"wchar: >MAX_PATH path keeps its length (+4 for the prefix)");
    check_eq(gotw,std::wstring(L"\\\\?\\")+longw,"wchar: >MAX_PATH path is prefixed, not truncated");
}

// ---------------------------------------------------------------------------------------
// 3) embedded NUL: length-delimited, never strlen()-cut  ("XXXXX\0XXXX")
// ---------------------------------------------------------------------------------------
static void test_embedded_nul()
{
    // "C:\XXXXX\0XXXX\f.txt" -- the NUL sits in the middle of a component
    std::string in("C:\\XXXXX");
    in.push_back('\0');
    in+="XXXX\\f.txt";
    const std::string got=TransferThread::toFinalPath(in);
    check(got.size()==in.size()+4,"char: embedded-NUL path keeps its exact size (no strlen cut)");
    check_eq(got,std::string("\\\\?\\")+in,"char: embedded-NUL path is copied byte-for-byte");
    check(got.find('\0')==4+8,"char: the NUL stays at its original offset");

    std::wstring inw(L"C:\\XXXXX");
    inw.push_back(L'\0');
    inw+=L"XXXX\\f.txt";
    const std::wstring gotw=TransferThread::toFinalPath(inw);
    check(gotw.size()==inw.size()+4,"wchar: embedded-NUL path keeps its exact size (no strlen cut)");
    check_eq(gotw,std::wstring(L"\\\\?\\")+inw,"wchar: embedded-NUL path is copied byte-for-byte");
    check(gotw.find(L'\0')==4+8,"wchar: the NUL stays at its original offset");

    // The sharp edge: a '/' AFTER the NUL. A strlen()/c_str() implementation stops scanning at
    // the NUL and leaves it as '/'; the length-delimited one converts it to '\'.
    std::string sep("C:/a");
    sep.push_back('\0');
    sep+="b/c.txt";
    std::string sep_expected("\\\\?\\C:\\a");
    sep_expected.push_back('\0');
    sep_expected+="b\\c.txt";
    check_eq(TransferThread::toFinalPath(sep),sep_expected,
             "char: separators AFTER an embedded NUL are still normalised (no strlen)");

    std::wstring sepw(L"C:/a");
    sepw.push_back(L'\0');
    sepw+=L"b/c.txt";
    std::wstring sepw_expected(L"\\\\?\\C:\\a");
    sepw_expected.push_back(L'\0');
    sepw_expected+=L"b\\c.txt";
    check_eq(TransferThread::toFinalPath(sepw),sepw_expected,
             "wchar: separators AFTER an embedded NUL are still normalised (no strlen)");

    // A NUL in the first two chars must not confuse the UNC/local decision either.
    std::string nulhead("\\\\nas");
    nulhead.push_back('\0');
    nulhead+="\\share\\f";
    check_eq(TransferThread::toFinalPath(nulhead),std::string("\\\\?\\UNC\\nas")+
             std::string(1,'\0')+"\\share\\f",
             "char: UNC detection is index-based, unaffected by a later NUL");
}

int main()
{
    test_prefix();
    test_over_max_path();
    test_embedded_nul();
    printf("%d checks, %d failure(s)\n",checks,failures);
    return failures==0?0:1;
}
