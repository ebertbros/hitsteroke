{* UltraStar Deluxe - Karaoke Game
 *
 * URemoteControl: local remote control ("pilot") for event/kiosk use.
 *
 * A non-blocking UDP socket bound to 127.0.0.1 is polled once per frame from
 * the main loop (main thread, no extra threads). Each datagram carries one
 * UTF-8 text command, the reply is a single JSON line sent back to the sender.
 *
 *   PLAY <song folder relative to a song directory>[/<file>.txt]
 *   STOP
 *   STATUS
 *   FINISH              ends the song now and shows the results
 *   SHOT [<file.jpg>]   saves what is on screen (operator / tests)
 *
 * Configuration lives in its own [Remote] section of config.ini. With
 * Enabled=0 (default) every hook in this unit is a no-op and the game
 * behaves exactly like upstream.
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *}

unit URemoteControl;

interface

{$IFDEF FPC}
  {$MODE Delphi}
{$ENDIF}

{$I switches.inc}

{ hooks called from upstream code; all of them are safe to call when the
  remote control is disabled or not initialized }

{ reads [Remote] from config.ini and opens the socket (call after Ini.Load) }
procedure RemoteInit;
{ processes incoming commands and drives pending actions (once per frame) }
procedure RemoteUpdate;
{ draws the waiting-screen overlay (after Display.Draw, before SwapBuffers) }
procedure RemoteDrawOverlay;
{ called at the start of TPartyGame.CallAfterSing; returns true if the remote
  control took over the screen change (e.g. skipping the score screen) }
function RemoteAfterSing: boolean;
{ volume of the song excerpt on the score screen (Default = upstream value) }
function RemoteScorePreviewVolume(Default: single): single;
{ closes the socket and frees resources }
procedure RemoteFinalize;

implementation

uses
  SysUtils,
  Classes,
  IniFiles,
  {$IFDEF UNIX}
  BaseUnix,
  Sockets,
  {$ENDIF}
  SDL2,
  UCommon,
  UDisplay,
  UGraphic,
  UHitsterUI,
  UImage,
  UIni,
  ULog,
  UMenu,
  UMusic,
  UNote,
  UPath,
  UPathUtils,
  UPlatform,
  URenderer,
  UScreenName,
  USong,
  USongs,
  UText,
  UUnicodeUtils;

const
  REMOTE_SECTION      = 'Remote';
  DEFAULT_PORT        = 47700;
  DEFAULT_SCORE_SECONDS = 15;
  DEFAULT_TEASER_VOLUME = 70;
  MAX_DATAGRAM        = 4096;
  LOG_SOURCE          = 'Remote';

type
  TRemoteAction = (raNone, raPlay, raStop, raFinish);

  TRemoteControl = class
  private
    FEnabled:      boolean;
    FSkipScore:    boolean;
    FScoreSeconds: integer;       // results shown this long, 0 = wait for a key
    FPort:         integer;
    FPlayers:      integer;
    FWaitingText:  UTF8String;
    FWaitingSub:   UTF8String;
    FDesign:       boolean;       // Hitster kiosk screens (UHitsterUI) instead of plain USDX
    FTeaserVolume: integer;       // score screen song excerpt, percent
    FArtDir:       UTF8String;    // art for UHitsterUI, empty = <user dir>/hitster

    FSocket:       integer;
    FSocketOpen:   boolean;

    FAction:       TRemoteAction; // pending action
    FPendingPath:  UTF8String;    // path of the pending PLAY
    FAbortToHome:  boolean;       // set while we abort a song ourselves
    FAwaitSing:    boolean;       // a song was started and should show up
    FHomeDone:     boolean;       // went to the waiting screen after startup
    FResultScreen: PMenu;         // results screen being timed (score / high scores)
    FResultT0:     cardinal;      // ticks when FResultScreen became current
    FScreen:       PMenu;         // current screen as seen by the last Step
    FScreenT0:     cardinal;      // ticks when FScreen became current
    FShotPath:     UTF8String;    // pending SHOT, saved after the next frame

    FCurrentPath:  UTF8String;    // folder of the song started by the remote
    FLastError:    UTF8String;
    FPlayT0:       cardinal;      // ticks when the last PLAY was received
    FLastStartMs:  integer;       // PLAY -> sing screen visible, in ms
    FStartCount:   integer;       // songs started by the remote so far

    procedure LoadConfig;
    procedure OpenSocket;
    procedure CloseSocket;

    procedure PollSocket;
    function  HandleCommand(const Line: UTF8String): UTF8String;
    function  CmdPlay(const Arg: UTF8String): UTF8String;
    function  CmdStop: UTF8String;
    function  CmdStatus: UTF8String;
    function  CmdShot(const Arg: UTF8String): UTF8String;
    procedure SaveShot;

    function  SongsReady: boolean;
    function  FindSong(const Request: UTF8String; out Error: UTF8String): integer;
    function  SongRelPath(Index: integer): UTF8String;

    procedure CloseOverlays;
    function  EnsureSongScreen(Cur: PMenu): boolean;
    procedure Step;
    procedure StartPendingSong;
  public
    constructor Create;
    destructor Destroy; override;

    procedure Update;
    procedure DrawOverlay;
    function  AfterSing: boolean;
    function  ScorePreviewVolume(Default: single): single;

    property Enabled: boolean read FEnabled;
  end;

var
  RemoteControl: TRemoteControl = nil;
  LastOverlayError: string = '';
  JsonFmt: TFormatSettings;

{ ---------------------------------------------------------------------------
  helpers
  --------------------------------------------------------------------------- }

function JsonStr(const S: UTF8String): UTF8String;
var
  I: integer;
  C: AnsiChar;
begin
  Result := '"';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    case C of
      '"':  Result := Result + '\"';
      '\':  Result := Result + '\\';
      #8:   Result := Result + '\b';
      #9:   Result := Result + '\t';
      #10:  Result := Result + '\n';
      #12:  Result := Result + '\f';
      #13:  Result := Result + '\r';
      #0..#7, #11, #14..#31:
            Result := Result + '\u' + IntToHex(Ord(C), 4);
    else
      Result := Result + C;
    end;
  end;
  Result := Result + '"';
end;

function JsonNum(Value: double): UTF8String;
begin
  Result := FormatFloat('0.000', Value, JsonFmt);
end;

function JsonBool(Value: boolean): UTF8String;
begin
  if Value then
    Result := 'true'
  else
    Result := 'false';
end;

{ normalizes a relative song path: '/' as separator, no leading/trailing
  separators, no surrounding whitespace }
function NormalizeRel(const S: UTF8String): UTF8String;
var
  I: integer;
begin
  Result := Trim(S);
  for I := 1 to Length(Result) do
    if Result[I] = '\' then
      Result[I] := '/';
  while (Length(Result) > 0) and (Result[1] = '/') do
    Delete(Result, 1, 1);
  while (Length(Result) > 0) and (Result[Length(Result)] = '/') do
    Delete(Result, Length(Result), 1);
end;

{ removes Unicode combining marks (U+0300..U+036F) so that NFC and NFD
  spellings of the same name compare equal after transliteration }
function StripCombiningMarks(const S: UTF8String): UTF8String;
var
  Src, Dst: UCS4String;
  I, N: integer;
begin
  Src := UTF8ToUCS4String(S);
  SetLength(Dst, Length(Src));
  N := 0;
  for I := 0 to High(Src) do
  begin
    if Src[I] = 0 then
      Break;
    if (Src[I] >= $0300) and (Src[I] <= $036F) then
      Continue;
    Dst[N] := Src[I];
    Inc(N);
  end;
  SetLength(Dst, N + 1);
  Dst[N] := 0;
  Result := UCS4ToUTF8String(Dst);
end;

function LooseKey(const S: UTF8String): UTF8String;
begin
  Result := LowerCase(TransliterateToASCII(StripCombiningMarks(S)));
end;

function IsScreen(Screen: PMenu; Target: pointer): boolean;
begin
  Result := (Screen <> nil) and (pointer(Screen) = Target);
end;

function CurrentScreenName: UTF8String;
var
  Cur: PMenu;
begin
  Cur := Display.CurrentScreen;
  if IsScreen(Cur, @ScreenSing) then Result := 'sing'
  else if IsScreen(Cur, @ScreenSong) then Result := 'song'
  else if IsScreen(Cur, @ScreenMain) then Result := 'main'
  else if IsScreen(Cur, @ScreenScore) then Result := 'score'
  else if IsScreen(Cur, @ScreenHighScores) then Result := 'highscores'
  else if IsScreen(Cur, @ScreenLoading) then Result := 'loading'
  else if IsScreen(Cur, @ScreenName) then Result := 'name'
  else if IsScreen(Cur, @ScreenEdit) then Result := 'edit'
  else if IsScreen(Cur, @ScreenJukebox) then Result := 'jukebox'
  else if IsScreen(Cur, @ScreenOptions) then Result := 'options'
  else if Cur = nil then Result := 'none'
  else Result := 'other';
end;

function ScreenChanging: boolean;
begin
  Result := (Display.NextScreen <> nil) or
            (Display.NextScreenWithCheck <> nil) or
            Display.CheckOK;
end;

{ ---------------------------------------------------------------------------
  TRemoteControl
  --------------------------------------------------------------------------- }

constructor TRemoteControl.Create;
begin
  inherited Create;
  FSocket := -1;
  FSocketOpen := false;
  FAction := raNone;
  FLastStartMs := -1;
  LoadConfig;
  if FEnabled then
    OpenSocket;
end;

destructor TRemoteControl.Destroy;
begin
  CloseSocket;
  inherited;
end;

procedure TRemoteControl.LoadConfig;
var
  IniFile: TMemIniFile;
  I: integer;
begin
  FEnabled := false;
  FSkipScore := false;
  FScoreSeconds := DEFAULT_SCORE_SECONDS;
  FPort := DEFAULT_PORT;
  FPlayers := 0;
  FWaitingText := '';
  FWaitingSub := '';
  FDesign := false;
  FTeaserVolume := DEFAULT_TEASER_VOLUME;
  FArtDir := '';

  if (Ini = nil) or (not Ini.Filename.IsSet) or (not Ini.Filename.IsFile) then
    Exit;

  IniFile := TMemIniFile.Create(Ini.Filename.ToNative);
  try
    FEnabled     := IniFile.ReadInteger(REMOTE_SECTION, 'Enabled', 0) = 1;
    FSkipScore   := IniFile.ReadInteger(REMOTE_SECTION, 'SkipScore', 0) = 1;
    FScoreSeconds := IniFile.ReadInteger(REMOTE_SECTION, 'ScoreSeconds', DEFAULT_SCORE_SECONDS);
    FPort        := IniFile.ReadInteger(REMOTE_SECTION, 'Port', DEFAULT_PORT);
    FPlayers     := IniFile.ReadInteger(REMOTE_SECTION, 'Players', 0);
    FWaitingText := IniFile.ReadString(REMOTE_SECTION, 'WaitingText', '');
    FWaitingSub  := IniFile.ReadString(REMOTE_SECTION, 'WaitingSubText', '');
    FDesign      := IniFile.ReadInteger(REMOTE_SECTION, 'Design', 1) = 1;
    FTeaserVolume := IniFile.ReadInteger(REMOTE_SECTION, 'TeaserVolume', DEFAULT_TEASER_VOLUME);
    FArtDir      := IniFile.ReadString(REMOTE_SECTION, 'ArtDir', '');
  finally
    IniFile.Free;
  end;

  if not FEnabled then
    Exit;

  if (FPort <= 0) or (FPort > 65535) then
    FPort := DEFAULT_PORT;
  if FScoreSeconds < 0 then
    FScoreSeconds := 0;
  if FTeaserVolume < 0 then FTeaserVolume := 0;
  if FTeaserVolume > 100 then FTeaserVolume := 100;

  if FDesign then
  begin
    if FArtDir <> '' then
      HitsterUISetArtDir(Path(FArtDir))
    else
      HitsterUISetArtDir(Platform.GetGameUserPath.Append('hitster'));
  end;

  // number of players for the event. It is written to [Game] Players too,
  // because the player selection screen reloads it from config.ini.
  if FPlayers > 0 then
  begin
    for I := Low(IPlayersVals) to High(IPlayersVals) do
      if (IPlayersVals[I] = FPlayers) and (Ini.Players <> I) then
      begin
        Ini.Players := I;
        Ini.SaveNumberOfPlayers;
      end;
  end;

  Log.LogStatus(Format('enabled: port=%d skipscore=%s scoreseconds=%d players=%d design=%s teaser=%d',
    [FPort, BoolToStr(FSkipScore, true), FScoreSeconds, FPlayers,
     BoolToStr(FDesign, true), FTeaserVolume]), LOG_SOURCE);
end;

procedure TRemoteControl.OpenSocket;
{$IFDEF UNIX}
var
  Addr: TInetSockAddr;
  Flags: cint;
{$ENDIF}
begin
{$IFDEF UNIX}
  FSocket := fpSocket(AF_INET, SOCK_DGRAM, 0);
  if FSocket < 0 then
  begin
    Log.LogError('cannot create UDP socket (errno ' + IntToStr(socketerror) + ')', LOG_SOURCE);
    Exit;
  end;

  FillChar(Addr, SizeOf(Addr), 0);
  Addr.sin_family := AF_INET;
  Addr.sin_port := htons(word(FPort));
  Addr.sin_addr := StrToNetAddr('127.0.0.1');

  if fpBind(FSocket, @Addr, SizeOf(Addr)) <> 0 then
  begin
    Log.LogError(Format('cannot bind UDP 127.0.0.1:%d (errno %d) - is another instance running?',
      [FPort, socketerror]), LOG_SOURCE);
    CloseSocket;
    Exit;
  end;

  Flags := fpfcntl(FSocket, F_GETFL);
  fpfcntl(FSocket, F_SETFL, Flags or O_NONBLOCK);

  FSocketOpen := true;
  Log.LogStatus(Format('listening on udp://127.0.0.1:%d', [FPort]), LOG_SOURCE);
{$ELSE}
  Log.LogError('remote control is only implemented for Unix-like systems', LOG_SOURCE);
{$ENDIF}
end;

procedure TRemoteControl.CloseSocket;
begin
{$IFDEF UNIX}
  if FSocket >= 0 then
    fpClose(FSocket);
{$ENDIF}
  FSocket := -1;
  FSocketOpen := false;
end;

procedure TRemoteControl.PollSocket;
{$IFDEF UNIX}
var
  Buf: array[0..MAX_DATAGRAM - 1] of AnsiChar;
  From: TInetSockAddr;
  FromLen: TSockLen;
  Received: ssize_t;
  Line, Reply: UTF8String;
  Count: integer;
{$ENDIF}
begin
{$IFDEF UNIX}
  if not FSocketOpen then
    Exit;

  // drain the queue, but never spend a whole frame on it
  for Count := 1 to 32 do
  begin
    FromLen := SizeOf(From);
    Received := fpRecvFrom(FSocket, @Buf[0], SizeOf(Buf), 0, @From, @FromLen);
    if Received <= 0 then
      Break; // EAGAIN / EWOULDBLOCK: nothing left

    SetString(Line, PAnsiChar(@Buf[0]), Received);
    Reply := HandleCommand(Line) + #10;
    fpSendTo(FSocket, PAnsiChar(Reply), Length(Reply), 0, @From, FromLen);
  end;
{$ENDIF}
end;

function TRemoteControl.HandleCommand(const Line: UTF8String): UTF8String;
var
  Cmd, Arg: UTF8String;
  P: integer;
begin
  Cmd := Trim(Line);
  // only the first line counts
  P := Pos(#10, Cmd);
  if P > 0 then
    Cmd := Trim(Copy(Cmd, 1, P - 1));

  P := Pos(' ', Cmd);
  if P > 0 then
  begin
    Arg := Trim(Copy(Cmd, P + 1, Length(Cmd)));
    Cmd := Copy(Cmd, 1, P - 1);
  end
  else
    Arg := '';
  Cmd := UpperCase(Cmd);

  if Cmd = 'PLAY' then
    Result := CmdPlay(Arg)
  else if Cmd = 'STOP' then
    Result := CmdStop
  else if (Cmd = 'STATUS') or (Cmd = 'PING') then
    Result := CmdStatus
  else if Cmd = 'SHOT' then
    Result := CmdShot(Arg)
  else if Cmd = 'FINISH' then
  begin
    if IsScreen(Display.CurrentScreen, @ScreenSing) then
    begin
      FAction := raFinish;
      Log.LogStatus('FINISH', LOG_SOURCE);
      Result := '{"ok":true,"cmd":"FINISH"}';
    end
    else
      Result := '{"ok":false,"cmd":"FINISH","error":"NOT_SINGING"}';
  end
  else
  begin
    Log.LogWarn('unknown command: ' + Cmd, LOG_SOURCE);
    Result := '{"ok":false,"cmd":' + JsonStr(Cmd) + ',"error":"BAD_COMMAND"}';
  end;
end;

function TRemoteControl.SongsReady: boolean;
begin
  Result := (Songs <> nil) and (not Songs.Processing) and
            (CatSongs <> nil) and (Length(CatSongs.Song) > 0) and
            (ScreenSong <> nil) and (ScreenName <> nil);
end;

function TRemoteControl.SongRelPath(Index: integer): UTF8String;
var
  SongDir, Base: UTF8String;
  I: integer;
begin
  Result := '';
  SongDir := NormalizeRel(CatSongs.Song[Index].Path.ToUTF8(false));
  if SongPaths = nil then
    Exit;
  for I := 0 to SongPaths.Count - 1 do
  begin
    Base := NormalizeRel((SongPaths[I] as IPath).ToUTF8(false));
    if (Base <> '') and (Length(SongDir) > Length(Base)) and
       (Copy(SongDir, 1, Length(Base) + 1) = Base + '/') then
    begin
      Result := Copy(SongDir, Length(Base) + 2, Length(SongDir));
      Exit;
    end;
  end;
end;

{ finds a song by folder (or folder/file.txt) relative to a song directory.
  Matching: exact, then case-insensitive, then accent-insensitive; the two
  loose levels only accept a unique match. Returns -1 if not found. }
function TRemoteControl.FindSong(const Request: UTF8String; out Error: UTF8String): integer;
var
  Req, ReqLower, ReqLoose, Rel, Candidate: UTF8String;
  WithFile: boolean;
  I, Level, Hits, Hit: integer;
begin
  Result := -1;
  Error := 'NOT_FOUND';
  Req := NormalizeRel(Request);
  if Req = '' then
  begin
    Error := 'EMPTY_PATH';
    Exit;
  end;

  WithFile := LowerCase(ExtractFileExt(Req)) = '.txt';
  ReqLower := UTF8LowerCase(Req);
  ReqLoose := LooseKey(Req);

  for Level := 0 to 2 do
  begin
    Hits := 0;
    Hit := -1;
    for I := 0 to High(CatSongs.Song) do
    begin
      if CatSongs.Song[I].Main then
        Continue;
      Rel := SongRelPath(I);
      if Rel = '' then
        Continue;
      if WithFile then
        Candidate := Rel + '/' + CatSongs.Song[I].FileName.ToUTF8(false)
      else
        Candidate := Rel;

      case Level of
        0: if Candidate = Req then begin Result := I; Exit; end;
        1: if UTF8LowerCase(Candidate) = ReqLower then begin Inc(Hits); Hit := I; end;
        2: if LooseKey(Candidate) = ReqLoose then begin Inc(Hits); Hit := I; end;
      end;
    end;

    if Hits = 1 then
    begin
      Result := Hit;
      Exit;
    end
    else if Hits > 1 then
    begin
      Error := 'AMBIGUOUS';
      Exit;
    end;
  end;
end;

function TRemoteControl.CmdPlay(const Arg: UTF8String): UTF8String;
var
  Index: integer;
  Err: UTF8String;
begin
  if not SongsReady then
  begin
    Result := '{"ok":false,"cmd":"PLAY","error":"BUSY","detail":"song list not loaded yet"}';
    Exit;
  end;

  Index := FindSong(Arg, Err);
  if Index < 0 then
  begin
    FLastError := Err + ': ' + Arg;
    Log.LogError('PLAY ' + Err + ': "' + Arg + '"', LOG_SOURCE);
    Result := '{"ok":false,"cmd":"PLAY","error":' + JsonStr(Err) +
              ',"song":' + JsonStr(Arg) + '}';
    Exit;
  end;

  // latest command wins
  FAction := raPlay;
  FPendingPath := SongRelPath(Index);
  if LowerCase(ExtractFileExt(NormalizeRel(Arg))) = '.txt' then
    FPendingPath := FPendingPath + '/' + CatSongs.Song[Index].FileName.ToUTF8(false);
  FPlayT0 := SDL_GetTicks();
  FLastError := '';
  Log.LogStatus('PLAY "' + FPendingPath + '"', LOG_SOURCE);

  Result := '{"ok":true,"cmd":"PLAY","song":' + JsonStr(FPendingPath) +
            ',"artist":' + JsonStr(CatSongs.Song[Index].Artist) +
            ',"title":' + JsonStr(CatSongs.Song[Index].Title) + '}';
end;

function TRemoteControl.CmdStop: UTF8String;
begin
  FAction := raStop;
  FPendingPath := '';
  Log.LogStatus('STOP', LOG_SOURCE);
  Result := '{"ok":true,"cmd":"STOP"}';
end;

function TRemoteControl.CmdStatus: UTF8String;
var
  ScreenName, State: UTF8String;
  OnSing: boolean;
  Position, Total, Progress: double;
  Artist, Title: UTF8String;
begin
  ScreenName := CurrentScreenName;
  OnSing := IsScreen(Display.CurrentScreen, @ScreenSing);

  if FAction <> raNone then
    State := 'switching'
  else if ScreenChanging then
    State := 'switching'
  else if OnSing and ScreenSing.Paused then
    State := 'paused'
  else if OnSing then
    State := 'playing'
  else if IsScreen(Display.CurrentScreen, @ScreenSong) then
    State := 'idle'
  else if IsScreen(Display.CurrentScreen, @ScreenScore) or
          IsScreen(Display.CurrentScreen, @ScreenHighScores) then
    State := 'results'
  else if not SongsReady then
    State := 'starting'
  else
    State := 'other';

  Position := 0;
  Total := 0;
  Progress := 0;
  Artist := '';
  Title := '';
  if OnSing then
  begin
    if AudioPlayback <> nil then
      Position := AudioPlayback.Position;
    if LyricsState <> nil then
      Total := LyricsState.TotalTime;
    if (Total <= 0) and (AudioPlayback <> nil) then
      Total := AudioPlayback.Length;
    if Total > 0 then
      Progress := Position / Total;
    if Progress < 0 then Progress := 0;
    if Progress > 1 then Progress := 1;
    if CurrentSong <> nil then
    begin
      Artist := CurrentSong.Artist;
      Title := CurrentSong.Title;
    end;
  end;

  Result := '{"ok":true,"cmd":"STATUS"' +
    ',"screen":' + JsonStr(ScreenName) +
    ',"state":' + JsonStr(State) +
    ',"song":' + JsonStr(FCurrentPath) +
    ',"artist":' + JsonStr(Artist) +
    ',"title":' + JsonStr(Title) +
    ',"time":' + JsonNum(Position) +
    ',"total":' + JsonNum(Total) +
    ',"progress":' + JsonNum(Progress) +
    ',"players":' + IntToStr(PlayersPlay) +
    ',"songs_ready":' + JsonBool(SongsReady) +
    ',"last_start_ms":' + IntToStr(FLastStartMs) +
    ',"starts":' + IntToStr(FStartCount);
  if FLastError <> '' then
    Result := Result + ',"last_error":' + JsonStr(FLastError)
  else
    Result := Result + ',"last_error":null';
  Result := Result + '}';
end;

function TRemoteControl.CmdShot(const Arg: UTF8String): UTF8String;
begin
  if Arg <> '' then
    FShotPath := Arg
  else if not ScreenshotsPath.IsUnset then
    FShotPath := ScreenshotsPath.Append(FormatDateTime('"hitster-"yyyymmdd-hhnnss".jpg"', Now)).ToUTF8
  else
  begin
    Result := '{"ok":false,"cmd":"SHOT","error":"NO_PATH"}';
    Exit;
  end;
  Result := '{"ok":true,"cmd":"SHOT","file":' + JsonStr(FShotPath) + '}';
end;

{ saves the frame that was just drawn (overlay included), without the
  "screenshot saved" popup of the upstream screenshot key }
procedure TRemoteControl.SaveShot;
var
  Data: PByte;
  RowSize: integer;
  Surface: PSDL_Surface;
  FileName: UTF8String;
begin
  FileName := FShotPath;
  FShotPath := '';
  Data := Renderer.GetFrameBufferData(RowSize);
  if Data = nil then
    Exit;
  Surface := SDL_CreateRGBSurfaceFrom(Data, ScreenW, ScreenH, 24, RowSize,
    $0000FF, $00FF00, $FF0000, 0);
  if WriteJPGImage(Path(FileName), Surface, 92) then
    Log.LogStatus('screenshot: ' + FileName, LOG_SOURCE)
  else
    Log.LogError('screenshot failed: ' + FileName, LOG_SOURCE);
  SDL_FreeSurface(Surface);
  FreeMem(Data);
end;

{ closes popups and song-screen menus that would otherwise swallow the
  screen change or stay on top of the next song }
procedure TRemoteControl.CloseOverlays;
begin
  if (ScreenPopupError <> nil) and ScreenPopupError.Visible then
  begin
    Log.LogWarn('closing error popup', LOG_SOURCE);
    ScreenPopupError.Visible := false;
  end;
  if (ScreenPopupInfo <> nil) and ScreenPopupInfo.Visible then
    ScreenPopupInfo.Visible := false;
  if (ScreenPopupCheck <> nil) and ScreenPopupCheck.Visible then
    ScreenPopupCheck.Visible := false;
  if (ScreenPopupHelp <> nil) and ScreenPopupHelp.Visible then
    ScreenPopupHelp.Visible := false;
  if (ScreenSongMenu <> nil) and ScreenSongMenu.Visible then
    ScreenSongMenu.Visible := false;
  if (ScreenSongJumpto <> nil) and ScreenSongJumpto.Visible then
    ScreenSongJumpto.Visible := false;
end;

{ moves towards the song screen; returns true once it is the current screen
  and a song can be started. Upstream creates the sing screen only when the
  player selection (ScreenName) is confirmed, so if it does not exist yet we
  go through ScreenName and confirm it with the players from config.ini. }
function TRemoteControl.EnsureSongScreen(Cur: PMenu): boolean;
begin
  Result := false;

  if ScreenSing = nil then
  begin
    ScreenName.Goto_SingScreen := false;
    if IsScreen(Cur, @ScreenName) then
    begin
      Log.LogStatus('confirming player selection', LOG_SOURCE);
      ScreenName.ParseInput(SDLK_RETURN, 0, true);
    end
    else
      Display.FadeTo(@ScreenName);
    Exit;
  end;

  if not IsScreen(Cur, @ScreenSong) then
  begin
    ScreenSong.Mode := smNormal;
    Display.FadeTo(@ScreenSong);
    Exit;
  end;

  Result := true;
end;

procedure TRemoteControl.StartPendingSong;
var
  Index: integer;
  Err: UTF8String;
begin
  // the song list may have been refreshed meanwhile: resolve again
  Index := FindSong(FPendingPath, Err);
  if Index < 0 then
  begin
    FLastError := Err + ': ' + FPendingPath;
    Log.LogError('song disappeared before start: "' + FPendingPath + '"', LOG_SOURCE);
    Exit;
  end;

  // make the song visible (search filter / category tabs)
  if not CatSongs.Song[Index].Visible then
  begin
    if Ini.Tabs = 1 then
      CatSongs.ShowCategory(CatSongs.Song[Index].OrderNum)
    else
      CatSongs.SetFilter('', fltAll);
    ScreenSong.HideCatTL;
  end;

  ScreenSong.SkipTo(CatSongs.VisibleIndex(Index), Index, CatSongs.VisibleSongs);
  if ScreenSong.Interaction <> Index then
  begin
    Log.LogWarn(Format('SkipTo landed on %d instead of %d, correcting',
      [ScreenSong.Interaction, Index]), LOG_SOURCE);
    ScreenSong.Interaction := Index;
  end;

  FCurrentPath := FPendingPath;
  FAwaitSing := true;
  ScreenSong.StartSong;
end;

{ one step of the action state machine; actions only happen while no
  screen transition is in progress }
procedure TRemoteControl.Step;
var
  Cur: PMenu;
begin
  Cur := Display.CurrentScreen;

  if Cur <> FScreen then
  begin
    // results -> anywhere but results: the song excerpt must not follow us
    // to the waiting screen (music only while singing and on the results)
    if (IsScreen(FScreen, @ScreenScore) or IsScreen(FScreen, @ScreenHighScores)) and
       not (IsScreen(Cur, @ScreenScore) or IsScreen(Cur, @ScreenHighScores)) and
       (AudioPlayback <> nil) then
      AudioPlayback.Stop;
    FScreen := Cur;
    FScreenT0 := SDL_GetTicks();
  end;

  // measure PLAY -> sing screen, detect failed song loads
  if FAwaitSing and not ScreenChanging then
  begin
    if IsScreen(Cur, @ScreenSing) then
    begin
      FAwaitSing := false;
      FLastStartMs := integer(SDL_GetTicks() - FPlayT0);
      Inc(FStartCount);
      Log.LogStatus(Format('song started in %d ms: "%s"', [FLastStartMs, FCurrentPath]), LOG_SOURCE);
    end
    else if IsScreen(Cur, @ScreenSong) then
    begin
      FAwaitSing := false;
      if (ScreenPopupError <> nil) and ScreenPopupError.Visible then
      begin
        FLastError := 'LOAD_FAILED: ' + FCurrentPath;
        Log.LogError('song could not be loaded: "' + FCurrentPath + '"', LOG_SOURCE);
        ScreenPopupError.Visible := false;
      end;
      FCurrentPath := '';
    end;
  end;

  // the "player N has no microphone" popup would cover the notes until the
  // next scan; on the stand it goes away after 3 s (the warning stays in the log)
  if IsScreen(Cur, @ScreenSing) and (ScreenPopupError <> nil) and
     ScreenPopupError.Visible and (SDL_GetTicks() - FScreenT0 > 3000) then
  begin
    Log.LogWarn('error popup on the sing screen closed (check Options > Record)', LOG_SOURCE);
    ScreenPopupError.Visible := false;
  end;

  // results (score, then high scores if someone pressed Enter): back to the
  // waiting screen after ScoreSeconds, so the stand never waits for a key
  if (FAction = raNone) and (FScoreSeconds > 0) and not ScreenChanging then
  begin
    if IsScreen(Cur, @ScreenScore) or IsScreen(Cur, @ScreenHighScores) then
    begin
      if Cur <> FResultScreen then
      begin
        FResultScreen := Cur;
        FResultT0 := SDL_GetTicks();
      end
      else if SDL_GetTicks() - FResultT0 >= cardinal(FScoreSeconds) * 1000 then
      begin
        Log.LogStatus('results shown, back to the waiting screen', LOG_SOURCE);
        FResultScreen := nil;
        ScreenSong.Mode := smNormal;
        Display.FadeTo(@ScreenSong);
        Exit;
      end;
    end
    else
      FResultScreen := nil;
  end;

  // event mode: after startup go from the main menu to the waiting screen
  if (not FHomeDone) and (FAction = raNone) and not ScreenChanging and SongsReady then
  begin
    if IsScreen(Cur, @ScreenSong) and (ScreenSing <> nil) then
      FHomeDone := true
    else if IsScreen(Cur, @ScreenMain) or IsScreen(Cur, @ScreenName) or
            IsScreen(Cur, @ScreenSong) then
    begin
      EnsureSongScreen(Cur);
      Exit;
    end;
  end;

  if FAction = raNone then
    Exit;
  if ScreenChanging then
    Exit;
  if not SongsReady then
    Exit;

  CloseOverlays;

  // FINISH: end the song like it ended by itself (results follow)
  if FAction = raFinish then
  begin
    FAction := raNone;
    if IsScreen(Cur, @ScreenSing) and not ScreenSing.FadeOut then
      ScreenSing.Finish;
    Exit;
  end;

  // 1. a song is running: stop it, CallAfterSing brings us to the song screen
  if IsScreen(Cur, @ScreenSing) then
  begin
    FAbortToHome := true;
    if ScreenSing.FadeOut then
      Display.FadeTo(@ScreenSong) // already finishing, just redirect
    else
      ScreenSing.Finish;
    FCurrentPath := '';
    Exit;
  end;

  // 2. any other screen: go to the song screen first
  if not EnsureSongScreen(Cur) then
    Exit;
  FHomeDone := true;

  // 3. on the song screen
  if ScreenSong.Mode <> smNormal then
    ScreenSong.Mode := smNormal;

  case FAction of
    raStop:
      begin
        FAction := raNone;
        FCurrentPath := '';
      end;
    raPlay:
      begin
        FAction := raNone;
        StartPendingSong;
      end;
  end;
end;

procedure TRemoteControl.Update;
begin
  if not FEnabled then
    Exit;
  PollSocket;
  Step;
end;

procedure TRemoteControl.DrawOverlay;
const
  BAR_Y = 470;
  BAR_H = 110;
var
  W: real;
  Cur: PMenu;
  Elapsed, Remaining: real;
begin
  if not FEnabled then
    Exit;

  if FDesign then
  begin
    Cur := Display.CurrentScreen;
    if Cur <> FScreen then
      Elapsed := 0
    else
      Elapsed := (SDL_GetTicks() - FScreenT0) / 1000;
    if IsScreen(Cur, @ScreenSing) then
      HitsterDrawSing(Elapsed)
    else if IsScreen(Cur, @ScreenScore) or IsScreen(Cur, @ScreenHighScores) then
    begin
      Remaining := -1;
      if FScoreSeconds > 0 then
        Remaining := FScoreSeconds - Elapsed;
      HitsterDrawScore(Elapsed, Remaining, FScoreSeconds);
    end
    // the waiting screen also hides the menus the game passes on startup
    else if IsScreen(Cur, @ScreenSong) or IsScreen(Cur, @ScreenLoading) or
            ((not FHomeDone) and (IsScreen(Cur, @ScreenMain) or IsScreen(Cur, @ScreenName))) then
      HitsterDrawWaiting(FWaitingText, FWaitingSub, Elapsed);
    Exit;
  end;

  if FWaitingText = '' then
    Exit;
  if (FAction <> raNone) or ScreenChanging then
    Exit;
  if not IsScreen(Display.CurrentScreen, @ScreenSong) then
    Exit;

  Renderer.DepthTest := false;
  Renderer.SetBlend(true);
  Renderer.DrawQuad(0, BAR_Y, 0, RenderW, BAR_H, 0, 0, 0, 0.75);

  SetFontFamily(0);
  SetFontStyle(ftBold);
  SetFontItalic(false);
  SetFontReflection(false, 0);
  SetFontSize(48);
  SetFontColor(1, 1, 1, 1);
  W := TextWidth(FWaitingText);
  SetFontPos((RenderW - W) / 2, BAR_Y + 12);
  PrintText(FWaitingText);

  if FWaitingSub <> '' then
  begin
    SetFontStyle(ftRegular);
    SetFontSize(26);
    SetFontColor(0.85, 0.85, 0.85, 1);
    W := TextWidth(FWaitingSub);
    SetFontPos((RenderW - W) / 2, BAR_Y + 66);
    PrintText(FWaitingSub);
  end;

  Renderer.DepthTest := true;
end;

function TRemoteControl.AfterSing: boolean;
begin
  Result := false;
  if not FEnabled then
    Exit;
  if (ScreenSong = nil) or (ScreenSong.Mode <> smNormal) then
    Exit;

  if FAbortToHome or FSkipScore then
  begin
    FAbortToHome := false;
    FCurrentPath := '';
    Display.FadeTo(@ScreenSong);
    Result := true;
  end;
end;

function TRemoteControl.ScorePreviewVolume(Default: single): single;
begin
  Result := Default;
  if FEnabled and FDesign then
    Result := FTeaserVolume / 100;
end;

{ ---------------------------------------------------------------------------
  hooks
  --------------------------------------------------------------------------- }

procedure RemoteInit;
begin
  if RemoteControl = nil then
    RemoteControl := TRemoteControl.Create;
end;

procedure RemoteUpdate;
begin
  if RemoteControl <> nil then
  try
    RemoteControl.Update;
  except
    on E: Exception do
      Log.LogError('update failed: ' + E.Message, LOG_SOURCE);
  end;
end;

procedure RemoteDrawOverlay;
begin
  if RemoteControl <> nil then
  try
    RemoteControl.DrawOverlay;
    if RemoteControl.FShotPath <> '' then
      RemoteControl.SaveShot;
  except
    // a drawing bug must never stop the kiosk; log it once per message
    on E: Exception do
      if E.Message <> LastOverlayError then
      begin
        LastOverlayError := E.Message;
        Log.LogError('overlay failed: ' + E.Message, LOG_SOURCE);
      end;
  end;
end;

function RemoteAfterSing: boolean;
begin
  Result := (RemoteControl <> nil) and RemoteControl.AfterSing;
end;

function RemoteScorePreviewVolume(Default: single): single;
begin
  if RemoteControl <> nil then
    Result := RemoteControl.ScorePreviewVolume(Default)
  else
    Result := Default;
end;

procedure RemoteFinalize;
begin
  HitsterUIFree;
  FreeAndNil(RemoteControl);
end;

initialization
  JsonFmt := DefaultFormatSettings;
  JsonFmt.DecimalSeparator := '.';
  JsonFmt.ThousandSeparator := #0;

end.
